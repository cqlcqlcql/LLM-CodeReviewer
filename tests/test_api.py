import subprocess

from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.schemas import ReviewIssue
from app.services.diff_loader import load_repository_diff
from app.services.llm import DeepSeekReviewer, build_reviewer
from app.settings import Settings


client = TestClient(app)


def test_health_reports_deepseek():
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "provider": "deepseek"}


def test_build_reviewer_only_uses_deepseek():
    reviewer = build_reviewer(Settings(DEEPSEEK_API_KEY="test-key"))

    assert isinstance(reviewer, DeepSeekReviewer)


def test_direct_code_review_uses_injected_test_reviewer():
    response = client.post(
        "/api/review",
        json={"language": "python", "code": "def add(a, b):\n    return a - b\n"},
    )

    assert response.status_code == 200
    issue = response.json()["issues"][0]
    assert issue["category"] == "logic_bug"
    assert issue["source"] == "LLM"


def test_single_file_review_uses_isolated_git_diff():
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "false",
        },
        files={"file": ("calculator.py", b"def add(a, b):\n    return a - b\n", "text/x-python")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issues"][0]["file_path"] == "calculator.py"
    assert data["issues"][0]["line"] == 1
    assert data["notices"] == [
        "Uploaded source and 0 related file(s) were reviewed in an isolated temporary Git workspace."
    ]


def test_single_file_pytest_no_tests_is_not_failure():
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "true",
        },
        files={"file": ("calculator.py", b"def add(a, b):\n    return a + b\n", "text/x-python")},
    )

    assert response.status_code == 200
    result = response.json()["test_result"]
    assert result["test_status"] == "no_tests"
    assert result["collected_cases"] == 0
    assert result["failed_cases"] == 0


def test_single_file_review_runs_uploaded_related_pytest_file():
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "true",
        },
        files=[
            ("file", ("calculator.py", b"def value():\n    return 1\n", "text/x-python")),
            (
                "related_files",
                (
                    "test_calculator.py",
                    b"from calculator import value\n\ndef test_value():\n    assert value() == 2\n",
                    "text/x-python",
                ),
            ),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_result"]["test_status"] == "failed"
    assert data["test_result"]["collected_cases"] == 1
    assert data["test_result"]["failed_cases"] == 1
    assert data["issues"][0]["source"] == "pytest + LLM"
    assert data["notices"] == [
        "Uploaded source and 1 related file(s) were reviewed in an isolated temporary Git workspace."
    ]


def test_single_file_review_preserves_pytest_result_when_deepseek_fails(monkeypatch):
    class FailingReviewer:
        async def review_repository(self, language, diff_context, static_issues, test_result):
            raise HTTPException(status_code=502, detail="invalid structured JSON")

    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: FailingReviewer())
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "true",
        },
        files=[
            ("file", ("calculator.py", b"def value():\n    return 1\n", "text/x-python")),
            (
                "related_files",
                (
                    "test_calculator.py",
                    b"from calculator import value\n\ndef test_value():\n    assert value() == 2\n",
                    "text/x-python",
                ),
            ),
        ],
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_result"]["test_status"] == "failed"
    assert data["test_result"]["collected_cases"] == 1
    assert data["test_result"]["failed_cases"] == 1
    assert "DeepSeek 综合分析暂不可用" in data["summary"]
    assert data["notices"] == [
        "Uploaded source and 1 related file(s) were reviewed in an isolated temporary Git workspace.",
        "DeepSeek 综合分析暂时失败；已保留本地静态分析和自动化测试结果。",
    ]


def test_single_file_review_rejects_duplicate_uploaded_names():
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "false",
        },
        files=[
            ("file", ("calculator.py", b"value = 1\n", "text/x-python")),
            ("related_files", ("calculator.py", b"value = 2\n", "text/x-python")),
        ],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded files must have unique file names"


def test_uploaded_filename_is_sanitized():
    response = client.post(
        "/api/review/file",
        data={
            "language": "python",
            "run_static_analysis": "false",
            "run_tests": "false",
        },
        files={"file": ("../unsafe calculator.py", b"def add(a, b):\n    return a - b\n", "text/x-python")},
    )

    assert response.status_code == 200
    assert response.json()["issues"][0]["file_path"] == "unsafe_calculator.py"


def test_review_repository_uses_configured_base_branch(tmp_path):
    _git(tmp_path, "init", "-b", "master")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "switch", "-c", "review")
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    _git(tmp_path, "add", "calculator.py")
    _git(tmp_path, "commit", "-m", "break add")

    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "base_branch": "master",
            "run_static_analysis": False,
        },
    )

    assert response.status_code == 200
    issue = response.json()["issues"][0]
    assert issue["file_path"] == "calculator.py"
    assert issue["line"] == 2


def test_repository_diff_returns_changed_file_summary(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "example.py")
    _git(tmp_path, "commit", "-m", "initial")
    _git(tmp_path, "switch", "-c", "review")
    source.write_text("value = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "example.py")
    _git(tmp_path, "commit", "-m", "change value")

    response = client.post(
        "/api/diff",
        json={"repository_path": str(tmp_path), "base_branch": "main"},
    )

    assert response.status_code == 200
    assert response.json()["files"] == [
        {"path": "example.py", "additions": 1, "deletions": 1, "hunks": 1}
    ]


def test_non_git_directory_skips_full_source_scan(tmp_path, monkeypatch):
    (tmp_path / "large_source.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("non-Git source scanning must not run")

    monkeypatch.setattr("app.services.code_loader.load_repository_code", fail_if_called)
    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "run_static_analysis": False,
            "run_tests": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issues"] == []
    assert data["summary"] == "No review evidence was collected."
    assert data["notices"] == [
        "Path is not a Git repository; skipped Git diff review.",
        "Full-source scanning was skipped for this non-Git directory.",
    ]


def test_non_git_directory_can_use_static_and_test_evidence(tmp_path, monkeypatch):
    (tmp_path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_calculator.py").write_text(
        "from calculator import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        main_module,
        "run_static_analysis",
        lambda repository_path, language: [
            ReviewIssue(
                file_path="calculator.py",
                source="ruff",
                severity="low",
                category="F401",
                line=1,
                message="Example tool issue.",
                suggestion="Resolve the tool finding.",
            )
        ],
    )
    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "run_static_analysis": True,
            "run_tests": True,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["issues"][0]["source"] == "ruff"
    assert data["test_result"]["test_status"] == "passed"
    assert data["test_result"]["collected_cases"] == 1
    assert data["test_result"]["passed_cases"] == 1


def test_pytest_failure_returns_counts_and_explanation(tmp_path):
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/test",
        json={"repository_path": str(tmp_path), "language": "python", "timeout_seconds": 60},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_status"] == "failed"
    assert data["collected_cases"] == 1
    assert data["failed_cases"] == 1
    assert "test_failure" in data["log_excerpt"]
    assert data["llm_explanation"]


def test_test_counts_include_skipped_cases(tmp_path):
    (tmp_path / "test_cases.py").write_text(
        "import pytest\n\ndef test_ok():\n    assert True\n\n@pytest.mark.skip(reason='demo')\ndef test_skip():\n    pass\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/test",
        json={"repository_path": str(tmp_path), "language": "python", "timeout_seconds": 60},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_status"] == "passed"
    assert data["collected_cases"] == 2
    assert data["passed_cases"] == 1
    assert data["skipped_cases"] == 1


def test_pytest_collection_error_is_not_reported_as_no_tests(tmp_path):
    (tmp_path / "test_import_error.py").write_text(
        "import module_that_does_not_exist\n",
        encoding="utf-8",
    )

    response = client.post(
        "/api/test",
        json={"repository_path": str(tmp_path), "language": "python", "timeout_seconds": 60},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["test_status"] == "failed"
    assert data["collected_cases"] == 0
    assert data["error_cases"] == 1


def test_review_requires_code_or_repository():
    response = client.post("/api/review", json={"language": "python"})

    assert response.status_code == 422


def test_load_repository_diff_rejects_non_git_directory(tmp_path):
    try:
        load_repository_diff(str(tmp_path), 24000, "main")
    except Exception as exc:
        assert "not a Git repository" in str(exc)
    else:
        raise AssertionError("non-Git directory should not produce a Git diff")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
