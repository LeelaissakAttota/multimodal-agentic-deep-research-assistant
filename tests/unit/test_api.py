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


def test_research_submission_and_status_lookup_are_bounded():
    response = client.post(
        "/research",
        json={"objective": "Deterministic battery reliability research"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["report"]["evidence_gathered"]
    assert data["report"]["sources_consulted"]
    assert data["runtime"]["usage"]["iterations_started"] == 1

    lookup = client.get(f"/research/{data['session_id']}")
    assert lookup.status_code == 200
    assert lookup.json() == data


def test_research_api_validates_input_and_returns_not_found():
    invalid = client.post("/research", json={"objective": "   "})
    missing = client.get("/research/00000000-0000-0000-0000-000000000001")

    assert invalid.status_code == 422
    assert missing.status_code == 404
