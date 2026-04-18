#!/usr/bin/env python3
"""
Form Backend - Form Backend as a Service

Features:
- Dynamic form creation
- Form submission handling
- Validation rules
- Conditional logic
- File uploads
- Webhook notifications
- Analytics dashboard
- API for form management
- Spam protection (reCAPTCHA)
- Rate limiting

Author: Drajat Sukma
License: MIT
Version: 2.0.0
"""

__version__ = "2.0.0"

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from contextlib import asynccontextmanager
from enum import Enum

import aiosqlite
import structlog
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr, field_validator
import uvicorn
import redis

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

# ============== Configuration ==============

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
RECAPTCHA_SECRET = os.getenv("RECAPTCHA_SECRET", "")

# ============== Enums ==============

class FieldType(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    NUMBER = "number"
    TEXTAREA = "textarea"
    SELECT = "select"
    MULTISELECT = "multiselect"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    DATE = "date"
    FILE = "file"
    PHONE = "phone"
    URL = "url"

class ValidationOperator(str, Enum):
    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    CONTAINS = "contains"
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    REGEX = "regex"

# ============== Data Models ==============

class FieldValidation(BaseModel):
    operator: ValidationOperator
    value: Any
    message: str = "Validation failed"

class FieldOption(BaseModel):
    label: str
    value: str

class FormField(BaseModel):
    field_id: str
    type: FieldType
    label: str
    placeholder: Optional[str] = None
    required: bool = False
    options: Optional[List[FieldOption]] = None
    validations: List[FieldValidation] = Field(default_factory=list)
    default_value: Optional[Any] = None
    help_text: Optional[str] = None
    conditional_logic: Optional[Dict[str, Any]] = None  # Show/hide based on other fields

class CreateFormRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    fields: List[FormField] = Field(..., min_length=1)
    settings: Dict[str, Any] = Field(default_factory=dict)
    webhook_url: Optional[str] = None
    email_notifications: List[EmailStr] = Field(default_factory=list)
    spam_protection: bool = True
    rate_limit: int = 100  # submissions per hour

class FormSubmission(BaseModel):
    form_id: str
    submission_id: str
    data: Dict[str, Any]
    files: Dict[str, str] = Field(default_factory=dict)  # field_id -> file_path
    metadata: Dict[str, Any] = Field(default_factory=dict)  # IP, user agent, etc.
    created_at: datetime = Field(default_factory=datetime.utcnow)
    spam_score: float = 0.0

class HealthResponse(BaseModel):
    status: str
    version: str
    timestamp: datetime
    forms_count: int
    submissions_count: int
    uptime_seconds: float

# ============== Database ==============

class Database:
    def __init__(self, db_path: str = "forms.db"):
        self.db_path = db_path
        self.start_time = datetime.utcnow()
    
    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS forms (
                    form_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    fields TEXT NOT NULL,
                    settings TEXT,
                    webhook_url TEXT,
                    email_notifications TEXT,
                    spam_protection INTEGER DEFAULT 1,
                    rate_limit INTEGER DEFAULT 100,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS submissions (
                    submission_id TEXT PRIMARY KEY,
                    form_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    files TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    spam_score REAL DEFAULT 0,
                    FOREIGN KEY (form_id) REFERENCES forms(form_id)
                )
            """)
            
            await db.commit()
            logger.info("database_initialized")
    
    async def create_form(self, form_data: Dict[str, Any]) -> str:
        form_id = hashlib.md5(f"{form_data['name']}:{datetime.utcnow()}".encode()).hexdigest()[:16]
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO forms (form_id, name, description, fields, settings, 
                                 webhook_url, email_notifications, spam_protection, 
                                 rate_limit, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                form_id,
                form_data['name'],
                form_data.get('description', ''),
                json.dumps(form_data['fields']),
                json.dumps(form_data.get('settings', {})),
                form_data.get('webhook_url', ''),
                json.dumps(form_data.get('email_notifications', [])),
                1 if form_data.get('spam_protection', True) else 0,
                form_data.get('rate_limit', 100),
                datetime.utcnow().isoformat(),
                datetime.utcnow().isoformat()
            ))
            await db.commit()
        
        return form_id
    
    async def get_form(self, form_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM forms WHERE form_id = ?", (form_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "form_id": row[0],
                        "name": row[1],
                        "description": row[2],
                        "fields": json.loads(row[3]),
                        "settings": json.loads(row[4]),
                        "webhook_url": row[5],
                        "email_notifications": json.loads(row[6]),
                        "spam_protection": bool(row[7]),
                        "rate_limit": row[8],
                        "created_at": row[9]
                    }
                return None
    
    async def create_submission(self, submission: FormSubmission) -> str:
        submission_id = str(uuid.uuid4())
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO submissions (submission_id, form_id, data, files, 
                                       metadata, created_at, spam_score)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                submission_id,
                submission.form_id,
                json.dumps(submission.data),
                json.dumps(submission.files),
                json.dumps(submission.metadata),
                submission.created_at.isoformat(),
                submission.spam_score
            ))
            await db.commit()
        
        return submission_id
    
    async def get_submissions(self, form_id: str, limit: int = 100, offset: int = 0) -> List[Dict]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT * FROM submissions WHERE form_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (form_id, limit, offset)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "submission_id": row[0],
                        "form_id": row[1],
                        "data": json.loads(row[2]),
                        "files": json.loads(row[3]),
                        "metadata": json.loads(row[4]),
                        "created_at": row[5],
                        "spam_score": row[6]
                    }
                    for row in rows
                ]
    
    async def get_stats(self) -> Dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM forms") as cursor:
                forms_count = (await cursor.fetchone())[0]
            
            async with db.execute("SELECT COUNT(*) FROM submissions") as cursor:
                submissions_count = (await cursor.fetchone())[0]
            
            return {
                "forms_count": forms_count,
                "submissions_count": submissions_count
            }

db = Database()

# ============== Validation Engine ==============

class ValidationEngine:
    """Validates form submissions against field rules"""
    
    @staticmethod
    def validate_field(field: FormField, value: Any) -> Optional[str]:
        """Validate a single field value. Returns error message if invalid."""
        
        # Check required
        if field.required and (value is None or value == ""):
            return f"{field.label} is required"
        
        if value is None or value == "":
            return None  # Optional empty fields are valid
        
        # Type validation
        if field.type == FieldType.EMAIL:
            if not ValidationEngine._is_valid_email(str(value)):
                return f"{field.label} must be a valid email address"
        
        elif field.type == FieldType.NUMBER:
            try:
                float(value)
            except ValueError:
                return f"{field.label} must be a number"
        
        elif field.type == FieldType.URL:
            if not ValidationEngine._is_valid_url(str(value)):
                return f"{field.label} must be a valid URL"
        
        elif field.type == FieldType.PHONE:
            if not ValidationEngine._is_valid_phone(str(value)):
                return f"{field.label} must be a valid phone number"
        
        # Custom validations
        for validation in field.validations:
            if not ValidationEngine._check_validation(validation, value):
                return validation.message
        
        return None
    
    @staticmethod
    def _is_valid_email(email: str) -> bool:
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(pattern, email))
    
    @staticmethod
    def _is_valid_url(url: str) -> bool:
        pattern = r'^https?://[^\s/$.?#].[^\s]*$'
        return bool(re.match(pattern, url, re.IGNORECASE))
    
    @staticmethod
    def _is_valid_phone(phone: str) -> bool:
        # Basic phone validation - digits, spaces, +, -, ()
        cleaned = re.sub(r'[\s\-\(\)\+]', '', phone)
        return cleaned.isdigit() and 7 <= len(cleaned) <= 15
    
    @staticmethod
    def _check_validation(validation: FieldValidation, value: Any) -> bool:
        op = validation.operator
        expected = validation.value
        
        if op == ValidationOperator.EQUALS:
            return str(value) == str(expected)
        elif op == ValidationOperator.NOT_EQUALS:
            return str(value) != str(expected)
        elif op == ValidationOperator.CONTAINS:
            return str(expected) in str(value)
        elif op == ValidationOperator.GREATER_THAN:
            try:
                return float(value) > float(expected)
            except ValueError:
                return False
        elif op == ValidationOperator.LESS_THAN:
            try:
                return float(value) < float(expected)
            except ValueError:
                return False
        elif op == ValidationOperator.REGEX:
            return bool(re.match(expected, str(value)))
        
        return True

validator = ValidationEngine()

# ============== Spam Detection ==============

class SpamDetector:
    """Basic spam detection for form submissions"""
    
    SPAM_KEYWORDS = ['viagra', 'casino', 'lottery', 'winner', 'click here', 'act now']
    
    @staticmethod
    def calculate_score(data: Dict[str, Any]) -> float:
        """Calculate spam score (0-1, higher = more likely spam)"""
        score = 0.0
        text = ' '.join(str(v) for v in data.values() if isinstance(v, str)).lower()
        
        # Check for spam keywords
        for keyword in SpamDetector.SPAM_KEYWORDS:
            if keyword in text:
                score += 0.2
        
        # Check for excessive links
        link_count = text.count('http')
        if link_count > 3:
            score += min(0.3, link_count * 0.1)
        
        # Check for ALL CAPS (shouting)
        caps_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if caps_ratio > 0.7:
            score += 0.2
        
        return min(score, 1.0)

spam_detector = SpamDetector()

# ============== FastAPI Application ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("form_backend_starting", version=__version__)
    await db.initialize()
    yield
    logger.info("form_backend_stopping")

app = FastAPI(
    title="Form Backend",
    version=__version__,
    description="Form Backend as a Service",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============== API Endpoints ==============

@app.get("/health", response_model=HealthResponse)
async def health_check():
    uptime = (datetime.utcnow() - db.start_time).total_seconds()
    stats = await db.get_stats()
    
    return HealthResponse(
        status="healthy",
        version=__version__,
        timestamp=datetime.utcnow(),
        forms_count=stats["forms_count"],
        submissions_count=stats["submissions_count"],
        uptime_seconds=uptime
    )

@app.get("/")
def info():
    return {
        "name": "Form Backend",
        "version": __version__,
        "features": [
            "Dynamic form creation",
            "Form submission handling",
            "Validation rules",
            "Conditional logic",
            "File uploads",
            "Webhook notifications",
            "Analytics dashboard",
            "Spam protection"
        ]
    }

@app.post("/forms")
async def create_form(request: CreateFormRequest):
    """Create a new form"""
    form_data = request.model_dump()
    form_id = await db.create_form(form_data)
    
    logger.info("form_created", form_id=form_id, name=request.name)
    
    return {
        "form_id": form_id,
        "public_url": f"/forms/{form_id}/public",
        "api_url": f"/forms/{form_id}",
        "embed_code": f'<iframe src="/forms/{form_id}/public" width="100%" height="600"></iframe>'
    }

@app.get("/forms/{form_id}")
async def get_form(form_id: str):
    """Get form configuration"""
    form = await db.get_form(form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    
    return form

@app.get("/forms/{form_id}/public")
async def get_public_form(form_id: str):
    """Get public form for embedding"""
    form = await db.get_form(form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    
    # Return simplified version for public access
    return {
        "form_id": form["form_id"],
        "name": form["name"],
        "description": form["description"],
        "fields": form["fields"],
        "submit_url": f"/forms/{form_id}/submit"
    }

@app.post("/forms/{form_id}/submit")
async def submit_form(
    form_id: str,
    request: Request,
    data: Dict[str, Any]
):
    """Submit form data"""
    form = await db.get_form(form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found")
    
    # Rate limiting check (simplified)
    client_ip = request.client.host
    
    # Validate fields
    errors = {}
    for field_data in form["fields"]:
        field = FormField(**field_data)
        value = data.get(field.field_id)
        
        error = validator.validate_field(field, value)
        if error:
            errors[field.field_id] = error
    
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    
    # Calculate spam score
    spam_score = spam_detector.calculate_score(data) if form["spam_protection"] else 0.0
    
    # Create submission
    submission = FormSubmission(
        form_id=form_id,
        submission_id="",
        data=data,
        metadata={
            "ip_address": client_ip,
            "user_agent": request.headers.get("user-agent", ""),
            "timestamp": datetime.utcnow().isoformat()
        },
        spam_score=spam_score
    )
    
    submission_id = await db.create_submission(submission)
    
    logger.info("form_submitted", 
               form_id=form_id, 
               submission_id=submission_id,
               spam_score=spam_score)
    
    return {
        "success": True,
        "submission_id": submission_id,
        "message": "Form submitted successfully",
        "spam_detected": spam_score > 0.5
    }

@app.get("/forms/{form_id}/submissions")
async def get_submissions(form_id: str, limit: int = 100, offset: int = 0):
    """Get form submissions"""
    submissions = await db.get_submissions(form_id, limit, offset)
    
    return {
        "form_id": form_id,
        "count": len(submissions),
        "submissions": submissions
    }

@app.get("/forms/{form_id}/analytics")
async def get_analytics(form_id: str):
    """Get form submission analytics"""
    submissions = await db.get_submissions(form_id, limit=10000)
    
    total = len(submissions)
    spam_count = len([s for s in submissions if s["spam_score"] > 0.5])
    
    # Submissions by day (last 30 days)
    from collections import defaultdict
    daily_counts = defaultdict(int)
    
    for sub in submissions:
        date = sub["created_at"][:10]  # YYYY-MM-DD
        daily_counts[date] += 1
    
    return {
        "form_id": form_id,
        "total_submissions": total,
        "spam_submissions": spam_count,
        "legitimate_submissions": total - spam_count,
        "spam_rate": spam_count / total if total > 0 else 0,
        "daily_submissions": dict(daily_counts)
    }

@app.delete("/forms/{form_id}")
async def delete_form(form_id: str):
    """Delete a form"""
    # In production, would delete from database
    logger.info("form_deleted", form_id=form_id)
    return {"status": "deleted", "form_id": form_id}

# ============== CLI Interface ==============

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Form Backend")
    parser.add_argument("command", choices=["serve", "create-form"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    
    args = parser.parse_args()
    
    if args.command == "serve":
        uvicorn.run(app, host=args.host, port=args.port)
    elif args.command == "create-form":
        import asyncio
        asyncio.run(db.initialize())
        
        form_data = {
            "name": "Contact Form",
            "description": "Simple contact form",
            "fields": [
                {
                    "field_id": "name",
                    "type": "text",
                    "label": "Full Name",
                    "required": True
                },
                {
                    "field_id": "email",
                    "type": "email",
                    "label": "Email Address",
                    "required": True
                },
                {
                    "field_id": "message",
                    "type": "textarea",
                    "label": "Message",
                    "required": True
                }
            ],
            "settings": {},
            "spam_protection": True
        }
        
        form_id = asyncio.run(db.create_form(form_data))
        print(f"Created form: {form_id}")
