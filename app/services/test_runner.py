import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastapi import HTTPException

from app.schemas import TestStatus, TestRunResponse as ProjectTestRunResponse
from app.services.llm import CodeReviewer


LOG_EXCERPT_CHARS = 6000


@dataclass(frozen=True)
class ProjectTestCommand:
    display: str
    args: list[str]


@dataclass(frozen=True)
class ProjectTestCounts:
    collected: int | None = None
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    errors: int | None = None


async def run_project_tests(
    repository_path: str,
    language: str | None,
    timeout_seconds: int,
    reviewer: CodeReviewer,
    explain_failures: bool = True,
) -> ProjectTestRunResponse:
    root = _resolve_project_root(repository_path)
    command = _detect_test_command(root, language)
    if command is None:
        return ProjectTestRunResponse(
            test_status="unsupported",
            command=None,
            failed_cases=None,
            log_excerpt="No supported Python, JavaScript/TypeScript, or Java test command was detected.",
            llm_explanation=None,
        )

    try:
        result = subprocess.run(
            command.args,
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        log_excerpt = _build_log_excerpt(exc.stdout, exc.stderr)
        return ProjectTestRunResponse(
            test_status="timeout",
            command=command.display,
            failed_cases=None,
            log_excerpt=log_excerpt or f"Test command timed out after {timeout_seconds} seconds.",
            llm_explanation=f"The test command exceeded the {timeout_seconds} second time limit.",
        )
    except OSError as exc:
        return ProjectTestRunResponse(
            test_status="error",
            command=command.display,
            failed_cases=None,
            log_excerpt=str(exc),
            llm_explanation=f"The test command could not be started: {exc}",
        )

    full_log = _combine_log(result.stdout, result.stderr)
    log_excerpt = _trim_log_excerpt(full_log)
    counts = _extract_test_counts(full_log)
    if _is_no_tests_result(command, result.returncode, full_log, counts):
        status = cast(TestStatus, "no_tests")
    else:
        status = cast(TestStatus, "passed" if result.returncode == 0 else "failed")
    failed_cases = 0 if status in {"passed", "no_tests"} else counts.failed
    explanation = None
    if status == "failed" and explain_failures:
        explanation = await reviewer.explain_test_failure(command.display, log_excerpt)

    return ProjectTestRunResponse(
        test_status=status,
        command=command.display,
        collected_cases=counts.collected,
        passed_cases=counts.passed,
        failed_cases=failed_cases,
        skipped_cases=counts.skipped,
        error_cases=counts.errors,
        log_excerpt=log_excerpt,
        llm_explanation=explanation,
    )


def _resolve_project_root(repository_path: str) -> Path:
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {repository_path}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {repository_path}")
    return root


def _detect_test_command(root: Path, language: str | None) -> ProjectTestCommand | None:
    normalized = (language or "").lower()
    if normalized in {"python", "py"} or _looks_like_python_project(root):
        return ProjectTestCommand("pytest", [sys.executable, "-m", "pytest"])
    if normalized in {"javascript", "typescript", "js", "ts"} or (root / "package.json").exists():
        return ProjectTestCommand("npm test", [_platform_command("npm"), "test"])
    if normalized == "java" or _looks_like_java_project(root):
        if (root / "pom.xml").exists():
            return ProjectTestCommand("mvn test", [_platform_command("mvn"), "test"])
        if (root / "gradlew").exists() or (root / "gradlew.bat").exists():
            wrapper = "gradlew.bat" if os.name == "nt" else "./gradlew"
            return ProjectTestCommand("gradle test", [str(root / wrapper), "test"])
        return ProjectTestCommand("gradle test", [_platform_command("gradle"), "test"])
    return None


def _looks_like_python_project(root: Path) -> bool:
    markers = (
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
    )
    if any((root / marker).exists() for marker in markers):
        return True
    return any(root.glob("tests/test_*.py")) or any(root.glob("test_*.py"))


def _looks_like_java_project(root: Path) -> bool:
    return any((root / marker).exists() for marker in ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew"))


def _platform_command(command: str) -> str:
    if os.name == "nt":
        return f"{command}.cmd"
    return command


def _build_log_excerpt(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return _trim_log_excerpt(_combine_log(stdout, stderr))


def _combine_log(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    return "\n".join(part for part in (_to_text(stdout), _to_text(stderr)) if part.strip()).strip()


def _trim_log_excerpt(log: str) -> str:
    if len(log) <= LOG_EXCERPT_CHARS:
        return log
    head_chars = LOG_EXCERPT_CHARS // 2
    tail_chars = LOG_EXCERPT_CHARS - head_chars
    return f"{log[:head_chars]}\n\n... test log truncated ...\n\n{log[-tail_chars:]}"


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _extract_failed_case_count(log_excerpt: str) -> int | None:
    patterns = (
        r"(?P<count>\d+)\s+failed",
        r"Tests run:\s*\d+,\s*Failures:\s*(?P<count>\d+)",
        r"(?P<count>\d+)\s+failing",
    )
    for pattern in patterns:
        match = re.search(pattern, log_excerpt, flags=re.IGNORECASE)
        if match:
            return int(match.group("count"))
    return None


def _extract_test_counts(log: str) -> ProjectTestCounts:
    collected = _extract_count(log, (r"collected\s+(?P<count>\d+)\s+items?", r"Tests run:\s*(?P<count>\d+)"))
    passed = _extract_count(log, (r"(?P<count>\d+)\s+passed\b", r"(?P<count>\d+)\s+passing\b"))
    failed = _extract_failed_case_count(log)
    skipped = _extract_count(log, (r"(?P<count>\d+)\s+skipped\b", r"(?P<count>\d+)\s+pending\b"))
    errors = _extract_count(log, (r"(?P<count>\d+)\s+errors?\b", r"Errors:\s*(?P<count>\d+)"))

    if collected is None:
        known_counts = [count for count in (passed, failed, skipped, errors) if count is not None]
        if known_counts:
            collected = sum(known_counts)

    if collected is not None:
        passed = passed or 0
        failed = failed or 0
        skipped = skipped or 0
        errors = errors or 0

    return ProjectTestCounts(
        collected=collected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
    )


def _extract_count(log: str, patterns: tuple[str, ...]) -> int | None:
    for pattern in patterns:
        matches = list(re.finditer(pattern, log, flags=re.IGNORECASE))
        if matches:
            return int(matches[-1].group("count"))
    return None


def _is_no_tests_result(
    command: ProjectTestCommand,
    returncode: int,
    log: str,
    counts: ProjectTestCounts,
) -> bool:
    if command.display != "pytest":
        return False
    lowered = log.lower()
    return (
        (counts.errors or 0) == 0
        and (
            counts.collected == 0
            or "no tests ran" in lowered
            or returncode == 5 and "collected 0 items" in lowered
        )
    )
