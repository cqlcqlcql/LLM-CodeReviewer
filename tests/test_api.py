from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_review_code_mock_detects_add_subtract_bug():
    response = client.post(
        "/api/review",
        json={"language": "python", "code": "def add(a,b): return a-b"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "函数名与实际行为不一致"
    assert data["issues"][0]["severity"] == "high"
    assert data["issues"][0]["category"] == "logic_bug"


def test_review_requires_code_or_repository():
    response = client.post("/api/review", json={"language": "python"})

    assert response.status_code == 422
