import subprocess
import os

os.environ["LLM_PROVIDER"] = "mock"

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.schemas import ReviewIssue
from app.services.static_analysis import run_static_analysis


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
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."
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


def test_repository_diff_returns_file_summary_and_unified_diff(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/review-me")

    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "break add")

    response = client.post(
        "/api/diff",
        json={"repository_path": str(tmp_path), "base_branch": "main"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["base_branch"] == "main"
    assert data["files"] == [{"path": "calculator.py", "additions": 1, "deletions": 1, "hunks": 1}]
    assert "+    return a - b" in data["diff"]
    assert "-    return a + b" in data["diff"]


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


def test_review_repository_merges_static_analysis_issues(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "checkout", "-b", "feature/review-me")

    source.write_text("def add(a, b):\n    unused = 1\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "add unused variable")

    def fake_static_analysis(repository_path, language):
        return [
            ReviewIssue(
                source="ruff",
                file_path="calculator.py",
                severity="low",
                category="F841",
                line=2,
                message="Local variable `unused` is assigned to but never used.",
                suggestion="Remove the unused variable.",
            )
        ]

    monkeypatch.setattr(main_module, "run_static_analysis", fake_static_analysis)

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path), "base_branch": "main"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issues"][0]["source"] == "ruff"
    assert data["issues"][0]["category"] == "F841"
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."


def test_review_repository_without_diff_can_still_run_static_analysis(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")

    def fake_static_analysis(repository_path, language):
        return [
            ReviewIssue(
                source="mypy",
                file_path="calculator.py",
                severity="medium",
                category="type_error",
                line=1,
                message="Example type issue.",
                suggestion="Add type annotations.",
            )
        ]

    monkeypatch.setattr(main_module, "run_static_analysis", fake_static_analysis)

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path), "base_branch": "main"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."
    assert data["notices"] == ["No diff found for main...HEAD; skipped Git diff review."]
    assert data["issues"][0]["source"] == "mypy"


def test_review_non_git_repository_can_still_run_static_analysis_and_tests(tmp_path, monkeypatch):
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    def fake_static_analysis(repository_path, language):
        return [
            ReviewIssue(
                source="ruff",
                file_path="calculator.py",
                severity="low",
                category="F401",
                line=1,
                message="Example lint issue.",
                suggestion="Remove the unused import.",
            )
        ]

    monkeypatch.setattr(main_module, "run_static_analysis", fake_static_analysis)

    response = client.post(
        "/api/review",
        json={"language": "python", "repository_path": str(tmp_path), "run_tests": True},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."
    assert data["notices"] == ["Path is not a Git repository; skipped Git diff review."]
    assert data["issues"][0]["source"] == "ruff"
    assert data["test_result"]["test_status"] == "passed"
    assert data["test_result"]["command"] == "pytest"


def test_static_analysis_does_not_merge_pytest_failures(tmp_path):
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    issues = run_static_analysis(str(tmp_path), "python")

    assert all(issue.source != "pytest" for issue in issues)


def test_review_non_git_repository_summarizes_test_failure_without_diff_text(tmp_path):
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "run_static_analysis": False,
            "run_tests": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."
    assert "diff" not in data["summary"].lower()
    assert "No review issues found" not in data["summary"]
    assert data["notices"] == ["Path is not a Git repository; skipped Git diff review."]
    assert data["issues"][0]["source"] == "pytest + LLM"
    assert data["issues"][0]["category"] == "test_failure"
    assert data["test_result"]["test_status"] == "failed"
    assert data["test_result"]["llm_explanation"] is None


def test_review_requires_code_or_repository():
    response = client.post("/api/review", json={"language": "python"})

    assert response.status_code == 422


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
