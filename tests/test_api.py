import subprocess
import os

os.environ["LLM_PROVIDER"] = "mock"

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.schemas import ReviewIssue
from app.schemas import ReviewResponse
from app.services.llm import _filter_review_issues
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


def test_review_non_git_repository_reviews_source_code_without_tools(tmp_path):
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

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
    assert data["summary"] == "Found 1 issue(s) from combined repository evidence."
    assert data["notices"] == ["Path is not a Git repository; skipped Git diff review."]
    assert data["issues"][0]["category"] == "logic_bug"


def test_static_analysis_does_not_merge_pytest_failures(tmp_path):
    (tmp_path / "test_failure.py").write_text(
        "def test_failure():\n    assert False\n",
        encoding="utf-8",
    )

    issues = run_static_analysis(str(tmp_path), "python")

    assert all(issue.source != "pytest" for issue in issues)


def test_review_file_normalizes_direct_review_source(monkeypatch):
    class FakeReviewer:
        async def review(self, language, code):
            return main_module.ReviewResponse(
                summary="Found one issue.",
                issues=[
                    ReviewIssue(
                        file_path=None,
                        source="test_name_that_should_not_be_a_source",
                        severity="low",
                        category="logic",
                        line=1,
                        message="Example issue.",
                        suggestion="Example suggestion.",
                    )
                ],
            )

    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: FakeReviewer())

    response = client.post(
        "/api/review/file",
        data={"language": "python"},
        files={"file": ("example.py", b"def example():\n    return 1\n", "text/x-python")},
    )

    assert response.status_code == 200
    assert response.json()["issues"][0]["source"] == "LLM"


def test_review_filter_removes_absent_operation_false_positive():
    code = "def bitcount(n):\n    count = 0\n    while n:\n        n &= n - 1\n        count += 1\n    return count\n"
    response = ReviewResponse(
        summary="Found one issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=3,
                message="The operation n ^= n - 1 cannot clear the lowest set bit and may loop forever.",
                suggestion="Use n &= n - 1.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert filtered.issues == []


def test_review_filter_keeps_real_operation_bug_and_removes_contract_noise():
    code = (
        "def bitcount(n):\n"
        "    count = 0\n"
        "    while n:\n"
        "        n ^= n - 1\n"
        "        count += 1\n"
        "    return count\n"
        "\n"
        '"""\n'
        "Input:\n"
        "    n: a nonnegative int\n"
        '"""\n'
    )
    response = ReviewResponse(
        summary="Found issues.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=4,
                message="n ^= n - 1 does not clear the lowest set bit for the documented algorithm.",
                suggestion="Use n &= n - 1.",
            ),
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="medium",
                category="runtime_exception",
                line=2,
                message="Negative numbers or None may produce unexpected behavior.",
                suggestion="Validate negative and non-integer inputs.",
            ),
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="low",
                category="maintainability",
                line=1,
                message="The function lacks a docstring.",
                suggestion="添加 docstring。",
            ),
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert [issue.message for issue in filtered.issues] == [
        "n ^= n - 1 does not clear the lowest set bit for the documented algorithm."
    ]


def test_review_filter_removes_generic_out_of_contract_input_comments():
    code = (
        "def first_digit(n):\n"
        '    """Input: n is a nonnegative integer."""\n'
        "    return int(str(n)[0])\n"
    )
    response = ReviewResponse(
        summary="Found issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="medium",
                category="runtime_exception",
                line=3,
                message="This can fail for negative numbers, floats, or None.",
                suggestion="Add input validation for negative and non-integer values.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert filtered.issues == []


def test_review_filter_keeps_tool_and_pytest_evidence_when_protected():
    response = ReviewResponse(
        summary="Found issues.",
        issues=[
            ReviewIssue(
                file_path="calculator.py",
                source="ruff",
                severity="low",
                category="F401",
                line=1,
                message="Imported name is unused.",
                suggestion="Remove the unused import.",
            ),
            ReviewIssue(
                file_path="calculator.py",
                source="LLM + pytest",
                severity="high",
                category="test_failure",
                line=2,
                message="Tests fail because add returns subtraction.",
                suggestion="Change the implementation to match the tests.",
            ),
        ],
    )

    filtered = _filter_review_issues("python", "", response, protect_tool_evidence=True)

    assert filtered.issues == response.issues


def test_review_filter_removes_graph_node_identity_speculation():
    code = (
        "def depth_first_search(startnode, goalnode):\n"
        "    nodesvisited = set()\n"
        "\n"
        "    def search_from(node):\n"
        "        if node in nodesvisited:\n"
        "            return False\n"
        "        elif node is goalnode:\n"
        "            return True\n"
        "        nodesvisited.add(node)\n"
        "        return any(search_from(nextnode) for nextnode in node.successors)\n"
        "\n"
        "    return search_from(startnode)\n"
        "\n"
        '"""\n'
        "Input:\n"
        "    startnode: A digraph node\n"
        "    goalnode: A digraph node\n"
        '"""\n'
    )
    response = ReviewResponse(
        summary="Found issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="medium",
                category="logic_bug",
                line=7,
                message="Using `is` to compare goalnode may fail for a different object with the same value.",
                suggestion="Use equality comparison instead.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert filtered.issues == []


def test_review_filter_keeps_missing_visited_update_bug():
    code = (
        "def depth_first_search(startnode, goalnode):\n"
        "    nodesvisited = set()\n"
        "\n"
        "    def search_from(node):\n"
        "        if node in nodesvisited:\n"
        "            return False\n"
        "        elif node is goalnode:\n"
        "            return True\n"
        "        return any(search_from(nextnode) for nextnode in node.successors)\n"
        "\n"
        "    return search_from(startnode)\n"
        "\n"
        '"""\n'
        "Input:\n"
        "    startnode: A digraph node\n"
        "    goalnode: A digraph node\n"
        '"""\n'
    )
    response = ReviewResponse(
        summary="Found issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=9,
                message="nodesvisited is never updated before recursion, so cyclic graphs can recurse forever.",
                suggestion="Add the current node to nodesvisited before visiting successors.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert [issue.message for issue in filtered.issues] == [
        "nodesvisited is never updated before recursion, so cyclic graphs can recurse forever."
    ]


def test_review_filter_removes_speculative_batch_false_positives():
    response = ReviewResponse(
        summary="Found issues.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="medium",
                category="runtime",
                line=2,
                message="If the input list is empty, arr[0] raises IndexError.",
                suggestion="Add input validation for empty input.",
            ),
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=9,
                message="This is not the standard LCS dynamic programming algorithm.",
                suggestion="Use standard dynamic programming and take max on mismatch.",
            ),
        ],
    )

    code = "def kth(arr, k):\n    pivot = arr[0]\n    return pivot\n"

    filtered = _filter_review_issues("python", code, response)

    assert filtered.issues == []


def test_review_filter_keeps_clear_behavior_bug_without_contract_noise():
    code = "def add(a, b):\n    return a - b\n"
    response = ReviewResponse(
        summary="Found issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=2,
                message="add returns subtraction instead of addition.",
                suggestion="Return a + b.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert len(filtered.issues) == 1


def test_review_filter_removes_empty_sublist_sum_contract_noise():
    code = (
        "def max_sublist_sum(arr):\n"
        "    return 0\n"
        '"""\n'
        "Efficient equivalent to max(sum(arr[i:j]) for 0 <= i <= j <= len(arr))\n"
        '"""\n'
    )
    response = ReviewResponse(
        summary="Found issue.",
        issues=[
            ReviewIssue(
                file_path=None,
                source="LLM",
                severity="high",
                category="logic_bug",
                line=2,
                message="When all elements are negative, this returns 0 instead of the largest negative value.",
                suggestion="Use Kadane's algorithm without allowing an empty sublist.",
            )
        ],
    )

    filtered = _filter_review_issues("python", code, response)

    assert filtered.issues == []


def test_direct_review_endpoint_applies_common_filter(monkeypatch):
    class FakeReviewer:
        async def review(self, language, code):
            return ReviewResponse(
                summary="Found issue.",
                issues=[
                    ReviewIssue(
                        file_path=None,
                        source="LLM",
                        severity="medium",
                        category="runtime",
                        line=2,
                        message="If the input list is empty, arr[0] raises IndexError.",
                        suggestion="Add input validation for empty input.",
                    )
                ],
            )

    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: FakeReviewer())

    response = client.post(
        "/api/review",
        json={"language": "python", "code": "def kth(arr, k):\n    return arr[0]\n"},
    )

    assert response.status_code == 200
    assert response.json()["issues"] == []


def test_git_repository_review_endpoint_applies_common_filter(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    source = tmp_path / "example.py"
    source.write_text("def value():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "example.py")
    _git(tmp_path, "commit", "-m", "initial")
    source.write_text("def value(items):\n    return items[0]\n", encoding="utf-8")

    class FakeReviewer:
        async def review_repository(self, language, diff_context, static_issues, test_result):
            return ReviewResponse(
                summary="Found issue.",
                issues=[
                    ReviewIssue(
                        file_path="example.py",
                        source="LLM",
                        severity="medium",
                        category="runtime",
                        line=2,
                        message="If the input list is empty, items[0] raises IndexError.",
                        suggestion="Add input validation for empty input.",
                    )
                ],
            )

    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: FakeReviewer())

    response = client.post(
        "/api/review",
        json={
            "language": "python",
            "repository_path": str(tmp_path),
            "base_branch": "main",
            "run_static_analysis": False,
            "run_tests": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["issues"] == []


def test_non_git_repository_review_endpoint_applies_common_filter(tmp_path, monkeypatch):
    (tmp_path / "example.py").write_text("def value(items):\n    return items[0]\n", encoding="utf-8")

    class FakeReviewer:
        async def review(self, language, code):
            return ReviewResponse(
                summary="Found issue.",
                issues=[
                    ReviewIssue(
                        file_path="example.py",
                        source="LLM",
                        severity="medium",
                        category="runtime",
                        line=2,
                        message="If the input list is empty, items[0] raises IndexError.",
                        suggestion="Add input validation for empty input.",
                    )
                ],
            )

    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: FakeReviewer())

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
    assert response.json()["issues"] == []


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
