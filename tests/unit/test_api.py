"""
Tests for the API endpoints.
"""
from fastapi.testclient import TestClient

from deep_research.api.main import app


client = TestClient(app)


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_check():
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_version_info():
    response = client.get("/version")
    assert response.status_code == 200
    data = response.json()
    assert "app_name" in data
    assert "version" in data
    assert "environment" in data
    # We can also check that the values match the settings if we want, but not required.
