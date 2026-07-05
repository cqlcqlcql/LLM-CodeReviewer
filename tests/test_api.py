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


def test_review_repository_uses_configured_base_branch(tmp_path):
    _git(tmp_path, "init", "-b", "master")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def subtract(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/review-me")

    source.write_text(
        "def subtract(a, b):\n    return a - b\n\n"
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "add broken add")

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path), "base_branch": "master"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "Found 1 issue(s) in changed lines."
    assert data["issues"][0]["file_path"] == "calculator.py"
    assert data["issues"][0]["line"] == 4
    assert data["issues"][0]["severity"] == "high"


def test_review_repository_uses_context_after_added_function(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def subtract(a, b):\n    return a - b", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/broken-add")

    source.write_text(
        "def subtract(a, b):\n    return a - b\n\n"
        "def add(a, b):\n    return a - b",
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
    assert data["issues"][0]["file_path"] == "calculator.py"
    assert data["issues"][0]["line"] == 4


def test_review_repository_rejects_invalid_base_branch(tmp_path):
    _git(tmp_path, "init", "-b", "main")

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path), "base_branch": "../main"},
    )

    assert response.status_code == 400
    assert "base_branch" in response.json()["detail"]


def test_run_project_tests_detects_python_failure(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_math.py").write_text(
        "def test_add():\n    assert 1 + 1 == 3\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/test",
        json={"repository_path": str(tmp_path), "language": "python", "timeout_seconds": 60},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_status"] == "failed"
    assert data["command"] == "pytest"
    assert data["failed_cases"] == 1
    assert "test_add" in data["log_excerpt"]
    assert data["llm_explanation"]


def test_review_repository_can_include_test_result(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/review-me")

    source.write_text("def add(a, b):\n    return a + b\n\n# TODO: add more edge cases\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "add todo")

    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "base_branch": "main",
            "run_tests": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_result"]["test_status"] == "passed"
    assert data["test_result"]["command"] == "pytest"


def test_review_requires_code_or_repository():
    response = client.post("/api/review", json={"language": "python"})

    assert response.status_code == 422


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
