import subprocess
import os

os.environ["LLM_PROVIDER"] = "mock"

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
    assert data["summary"] == "Function name does not match behavior."
    assert data["issues"][0]["file_path"] is None
    assert data["issues"][0]["severity"] == "high"
    assert data["issues"][0]["category"] == "logic_bug"


def test_review_repository_uses_main_head_diff(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def subtract(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/review-me")

    source.write_text(
        "def subtract(a, b):\n    return a - b\n\n"
        "def add(a,b): return a-b\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "add broken add")

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path)},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "Found 1 issue(s) in changed lines."
    assert data["issues"][0]["file_path"] == "calculator.py"
    assert data["issues"][0]["line"] == 4
    assert data["issues"][0]["severity"] == "high"


def test_review_requires_code_or_repository():
    response = client.post("/api/review", json={"language": "python"})

    assert response.status_code == 422


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
