import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fastapi import HTTPException

from app.schemas import ReviewIssue, Severity
from app.services.code_loader import trim_code


STATIC_LOG_EXCERPT_CHARS = 1600
IGNORED_PATHS = ".venv,venv,env,.git,.mypy_cache,.pytest_cache,__pycache__,.tmp"
MYPY_EXCLUDE_RE = r"(\.venv|venv|env|\.git|\.mypy_cache|\.pytest_cache|__pycache__|\.tmp)"
BANDIT_IGNORED_PATH_PARTS = ("\\.tmp\\", "/.tmp/", "\\tests\\", "/tests/")


@dataclass(frozen=True)
class StaticAnalysisCommand:
    source: str
    display: str
    args: list[str]


def run_static_analysis(
    repository_path: str,
    language: str | None,
    timeout_seconds: int = 60,
) -> list[ReviewIssue]:
    root = _resolve_project_root(repository_path)
    issues: list[ReviewIssue] = []

    for command in _detect_static_commands(root, language):
        result = _run_command(root, command, timeout_seconds)
        if result is None:
            continue
        issues.extend(_parse_command_issues(command, result))

    return issues


def _resolve_project_root(repository_path: str) -> Path:
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {repository_path}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {repository_path}")
    return root


def _detect_static_commands(root: Path, language: str | None) -> list[StaticAnalysisCommand]:
    commands: list[StaticAnalysisCommand] = []
    normalized = (language or "").lower()

    if normalized in {"python", "py"} or _looks_like_python_project(root):
        commands.extend(
            [
                StaticAnalysisCommand("ruff", "ruff check .", [sys.executable, "-m", "ruff", "check", ".", "--output-format", "json"]),
                StaticAnalysisCommand("mypy", "mypy .", [sys.executable, "-m", "mypy", ".", "--exclude", MYPY_EXCLUDE_RE]),
                StaticAnalysisCommand(
                    "bandit",
                    "bandit -r .",
                    [sys.executable, "-m", "bandit", "-r", ".", "-x", IGNORED_PATHS, "-s", "B101", "-f", "json"],
                ),
            ]
        )
    if (root / "package.json").exists() or normalized in {"javascript", "typescript", "js", "ts"}:
        commands.extend(
            [
                StaticAnalysisCommand("npm", "npm run lint", [_platform_command("npm"), "run", "lint"]),
            ]
        )

    return commands


def _run_command(root: Path, command: StaticAnalysisCommand, timeout_seconds: int) -> subprocess.CompletedProcess[str] | None:
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
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        return subprocess.CompletedProcess(command.args, 124, stdout or "", stderr or "")
    except OSError:
        return None

    if _is_missing_tool(command, result):
        return None
    return result


def _parse_command_issues(command: StaticAnalysisCommand, result: subprocess.CompletedProcess[str]) -> list[ReviewIssue]:
    if result.returncode == 0:
        return []

    if command.source == "ruff":
        return _parse_ruff_issues(result.stdout)
    if command.source == "mypy":
        return _parse_mypy_issues(result.stdout)
    if command.source == "bandit":
        return _parse_bandit_issues(result.stdout)
    if command.display == "npm run lint":
        return [_build_process_issue(command, result, "lint_failure", f"{command.display} failed.")]

    return [_build_process_issue(command, result, "tool_failure", f"{command.display} failed.")]


def _parse_ruff_issues(stdout: str) -> list[ReviewIssue]:
    try:
        diagnostics = json.loads(stdout or "[]")
    except json.JSONDecodeError:
        return []

    issues: list[ReviewIssue] = []
    for diagnostic in diagnostics:
        location = diagnostic.get("location") or {}
        issues.append(
            ReviewIssue(
                source="ruff",
                file_path=diagnostic.get("filename"),
                severity="low",
                category=diagnostic.get("code") or "lint",
                line=location.get("row"),
                message=diagnostic.get("message") or "Ruff reported a lint issue.",
                suggestion=_ruff_suggestion(diagnostic),
            )
        )
    return issues


def _parse_mypy_issues(stdout: str) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    pattern = re.compile(
        r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<column>\d+):)?\s*(?P<level>error|note):\s*(?P<message>.+)$"
    )
    for raw_line in stdout.splitlines():
        match = pattern.match(raw_line)
        if not match or match.group("level") != "error":
            continue
        message = match.group("message")
        code_match = re.search(r"\[(?P<code>[a-zA-Z0-9_-]+)\]$", message)
        issues.append(
            ReviewIssue(
                source="mypy",
                file_path=match.group("file"),
                severity="medium",
                category=code_match.group("code") if code_match else "type_error",
                line=int(match.group("line")),
                message=message,
                suggestion="Fix the type mismatch or add precise typing where the value is defined.",
            )
        )
    return issues


def _parse_bandit_issues(stdout: str) -> list[ReviewIssue]:
    try:
        report: dict[str, Any] = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return []

    issues: list[ReviewIssue] = []
    for item in report.get("results", []):
        filename = str(item.get("filename") or "")
        if _is_ignored_bandit_path(filename):
            continue
        issues.append(
            ReviewIssue(
                source="bandit",
                file_path=filename,
                severity=_bandit_severity(item.get("issue_severity")),
                category=item.get("test_id") or "security",
                line=item.get("line_number"),
                message=item.get("issue_text") or "Bandit reported a potential security issue.",
                suggestion="Review the flagged code path and replace it with the safer pattern recommended by Bandit.",
            )
        )
    return issues


def _is_ignored_bandit_path(filename: str) -> bool:
    normalized = filename.replace("/", "\\")
    return any(part.replace("/", "\\") in normalized for part in BANDIT_IGNORED_PATH_PARTS)


def _build_process_issue(
    command: StaticAnalysisCommand,
    result: subprocess.CompletedProcess[str],
    category: str,
    message: str,
) -> ReviewIssue:
    log_excerpt = _build_log_excerpt(result.stdout, result.stderr)
    detail = f"{message} Exit code: {result.returncode}."
    return ReviewIssue(
        source=command.source,
        file_path=None,
        severity=cast(Severity, "medium"),
        category=category,
        line=None,
        message=detail,
        suggestion=trim_code(log_excerpt, STATIC_LOG_EXCERPT_CHARS) or f"Run `{command.display}` locally for full output.",
    )


def _looks_like_python_project(root: Path) -> bool:
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "setup.py", "requirements.txt")
    return any((root / marker).exists() for marker in markers) or _has_python_tests(root)


def _has_python_tests(root: Path) -> bool:
    return any(root.glob("tests/test_*.py")) or any(root.glob("test_*.py"))


def _platform_command(command: str) -> str:
    if os.name == "nt":
        return f"{command}.cmd"
    return command


def _is_missing_tool(command: StaticAnalysisCommand, result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    missing_python_module = f"no module named {command.source}".lower()
    missing_npm_script = command.display.startswith("npm ") and "missing script:" in output
    return (
        missing_python_module in output
        or "is not recognized as an internal or external command" in output
        or "enoent" in output
        or missing_npm_script
    )


def _build_log_excerpt(stdout: str | None, stderr: str | None) -> str:
    return trim_code("\n".join(part for part in (stdout or "", stderr or "") if part.strip()).strip(), STATIC_LOG_EXCERPT_CHARS)


def _ruff_suggestion(diagnostic: dict[str, Any]) -> str:
    fix = diagnostic.get("fix")
    if isinstance(fix, dict) and fix.get("message"):
        return str(fix["message"])
    return "Apply the Ruff recommendation or adjust the code so this lint rule no longer fires."


def _bandit_severity(value: str | None) -> Severity:
    normalized = (value or "").lower()
    if normalized == "high":
        return "high"
    if normalized == "medium":
        return "medium"
    return "low"
