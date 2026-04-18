"""Tests for Form Backend"""

import pytest
from fastapi.testclient import TestClient

from app.main import app, db, ValidationEngine, SpamDetector, FormField, FieldType

client = TestClient(app)


class TestHealth:
    def test_health_check(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_info(self):
        response = client.get("/")
        assert response.status_code == 200
        assert "Form Backend" in response.json()["name"]


class TestForms:
    def test_create_form(self):
        request = {
            "name": "Test Form",
            "description": "A test form",
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
                    "label": "Email",
                    "required": True
                }
            ],
            "spam_protection": True,
            "rate_limit": 100
        }
        response = client.post("/forms", json=request)
        assert response.status_code == 200
        data = response.json()
        assert "form_id" in data
        assert "public_url" in data

    def test_get_form_not_found(self):
        response = client.get("/forms/nonexistent")
        assert response.status_code == 404

    def test_get_public_form(self):
        # Create form first
        create_response = client.post("/forms", json={
            "name": "Public Test",
            "fields": [{"field_id": "test", "type": "text", "label": "Test", "required": False}]
        })
        form_id = create_response.json()["form_id"]
        
        response = client.get(f"/forms/{form_id}/public")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Public Test"


class TestSubmissions:
    def test_submit_form_validation(self):
        # Create form with required field
        create_response = client.post("/forms", json={
            "name": "Validation Test",
            "fields": [
                {
                    "field_id": "email",
                    "type": "email",
                    "label": "Email",
                    "required": True
                }
            ]
        })
        form_id = create_response.json()["form_id"]
        
        # Submit without required field
        response = client.post(f"/forms/{form_id}/submit", json={})
        assert response.status_code == 422

    def test_submit_form_success(self):
        # Create form
        create_response = client.post("/forms", json={
            "name": "Success Test",
            "fields": [
                {
                    "field_id": "name",
                    "type": "text",
                    "label": "Name",
                    "required": False
                }
            ]
        })
        form_id = create_response.json()["form_id"]
        
        # Submit
        response = client.post(f"/forms/{form_id}/submit", json={"name": "John Doe"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "submission_id" in data


class TestValidation:
    def test_email_validation(self):
        field = FormField(
            field_id="email",
            type=FieldType.EMAIL,
            label="Email",
            required=True
        )
        
        # Valid email
        assert ValidationEngine.validate_field(field, "test@example.com") is None
        
        # Invalid email
        assert ValidationEngine.validate_field(field, "invalid-email") is not None
        
        # Required but empty
        assert ValidationEngine.validate_field(field, "") is not None

    def test_number_validation(self):
        field = FormField(
            field_id="age",
            type=FieldType.NUMBER,
            label="Age",
            required=False
        )
        
        # Valid number
        assert ValidationEngine.validate_field(field, "25") is None
        
        # Invalid
        assert ValidationEngine.validate_field(field, "not-a-number") is not None
        
        # Empty optional is fine
        assert ValidationEngine.validate_field(field, "") is None


class TestSpamDetection:
    def test_spam_score_calculation(self):
        # Legitimate data
        score = SpamDetector.calculate_score({
            "name": "John Doe",
            "message": "Hello, I would like to inquire about your services."
        })
        assert score < 0.3

    def test_spam_keywords(self):
        # Spammy data
        score = SpamDetector.calculate_score({
            "name": "Winner",
            "message": "Congratulations! You won the lottery! Click here to claim your prize."
        })
        assert score > 0.3
