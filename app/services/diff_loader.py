import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from app.services.code_loader import trim_code


DIFF_RANGE_RE = re.compile(r"@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")
BASE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


@dataclass
class DiffLine:
    kind: str
    old_line: int | None
    new_line: int | None
    content: str


@dataclass
class DiffHunk:
    header: str
    old_start: int
    new_start: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class ChangedFile:
    path: str
    hunks: list[DiffHunk] = field(default_factory=list)


def load_repository_diff(repository_path: str, max_chars: int, base_branch: str = "main") -> str:
    diff = load_repository_unified_diff(repository_path, base_branch)
    files = parse_unified_diff(diff)
    if not files:
        raise HTTPException(status_code=400, detail="Git diff did not contain reviewable file changes")

    return trim_code(format_diff_for_review(files), max_chars)


def load_repository_unified_diff(repository_path: str, base_branch: str = "main") -> str:
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {repository_path}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Path is not a directory: {repository_path}")
    if not (root / ".git").exists():
        raise HTTPException(status_code=400, detail=f"Path is not a Git repository: {repository_path}")

    normalized_base = _normalize_base_branch(base_branch)
    diff = _run_git_diff(root, normalized_base)
    if not diff.strip():
        raise HTTPException(status_code=400, detail=f"No diff found for {normalized_base}...HEAD")

    return diff


def parse_unified_diff(diff: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    current_file: ChangedFile | None = None
    current_hunk: DiffHunk | None = None
    old_line = 0
    new_line = 0

    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git "):
            current_file = None
            current_hunk = None
            continue

        if raw_line.startswith("+++ "):
            path = raw_line[4:]
            if path == "/dev/null":
                current_file = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            current_file = ChangedFile(path=path)
            files.append(current_file)
            current_hunk = None
            continue

        if raw_line.startswith("@@ ") and current_file is not None:
            match = DIFF_RANGE_RE.search(raw_line)
            if not match:
                current_hunk = None
                continue
            old_line = int(match.group("old_start"))
            new_line = int(match.group("new_start"))
            current_hunk = DiffHunk(header=raw_line, old_start=old_line, new_start=new_line)
            current_file.hunks.append(current_hunk)
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            current_hunk.lines.append(DiffLine("+", None, new_line, raw_line[1:]))
            new_line += 1
        elif raw_line.startswith("-") and not raw_line.startswith("---"):
            current_hunk.lines.append(DiffLine("-", old_line, None, raw_line[1:]))
            old_line += 1
        elif raw_line.startswith(" "):
            content = raw_line[1:]
            current_hunk.lines.append(DiffLine(" ", old_line, new_line, content))
            old_line += 1
            new_line += 1
        elif raw_line.startswith("\\"):
            continue

    return [changed_file for changed_file in files if changed_file.hunks]


def format_diff_for_review(files: list[ChangedFile]) -> str:
    blocks = [
        "Review only lines marked ADDED or MODIFIED. Context lines are included only to understand the change.",
        "Each changed line includes its new-file line number.",
    ]

    for changed_file in files:
        blocks.append(f"\nFILE: {changed_file.path}")
        for hunk in changed_file.hunks:
            blocks.append(f"HUNK: {hunk.header}")
            for line in hunk.lines:
                if line.kind == "+":
                    blocks.append(f"ADDED new_line={line.new_line}: {line.content}")
                elif line.kind == "-":
                    blocks.append(f"REMOVED old_line={line.old_line}: {line.content}")
                else:
                    blocks.append(
                        f"CONTEXT old_line={line.old_line} new_line={line.new_line}: {line.content}"
                    )

    return "\n".join(blocks)


def _normalize_base_branch(base_branch: str) -> str:
    value = base_branch.strip()
    if not value:
        raise HTTPException(status_code=400, detail="base_branch is required")
    if value.startswith("-") or ".." in value or not BASE_BRANCH_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="base_branch contains invalid characters")
    return value


def _run_git_diff(root: Path, base_branch: str) -> str:
    diff_range = f"{base_branch}...HEAD"
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "diff", diff_range],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=408, detail=f"git diff {diff_range} timed out") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to run git: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git diff {diff_range} failed"
        raise HTTPException(status_code=400, detail=detail)

    return result.stdout
