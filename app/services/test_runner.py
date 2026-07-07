import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from fastapi import HTTPException

from app.schemas import TestStatus, TestRunResponse as ProjectTestRunResponse
from app.services.code_loader import trim_code
from app.services.llm import CodeReviewer


LOG_EXCERPT_CHARS = 6000


@dataclass(frozen=True)
class ProjectTestCommand:
    display: str
    args: list[str]


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

    log_excerpt = _build_log_excerpt(result.stdout, result.stderr)
    status = cast(TestStatus, "passed" if result.returncode == 0 else "failed")
    failed_cases = 0 if status == "passed" else _extract_failed_case_count(log_excerpt)
    explanation = None
    if status == "failed" and explain_failures:
        explanation = await reviewer.explain_test_failure(command.display, log_excerpt)

    return ProjectTestRunResponse(
        test_status=status,
        command=command.display,
        failed_cases=failed_cases,
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
    log = "\n".join(part for part in (_to_text(stdout), _to_text(stderr)) if part.strip())
    return trim_code(log.strip(), LOG_EXCERPT_CHARS)


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
