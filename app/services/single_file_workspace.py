import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from typing import Iterator

from fastapi import HTTPException


# Keep uploaded Python files outside the application tree. Otherwise uvicorn's
# reload watcher can restart the server while a review is still running.
WORKSPACE_ROOT = Path(gettempdir()) / "llm-code-reviewer" / "reviews"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_RELATED_FILES = 20
MAX_TOTAL_BYTES = 5_000_000


@contextmanager
def single_file_repository(
    filename: str | None,
    content: bytes,
    related_files: list[tuple[str | None, bytes]] | None = None,
) -> Iterator[Path]:
    """Create an isolated repository containing one source file and optional related files."""
    uploads = [(filename, content), *(related_files or [])]
    if len(uploads) - 1 > MAX_RELATED_FILES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_RELATED_FILES} related files can be uploaded")
    if sum(len(file_content) for _, file_content in uploads) > MAX_TOTAL_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded review files exceed the 5 MB limit")

    prepared_files = [(_safe_filename(upload_name), file_content) for upload_name, file_content in uploads]
    safe_names = [safe_name for safe_name, _ in prepared_files]
    if len(safe_names) != len(set(safe_names)):
        raise HTTPException(status_code=400, detail="Uploaded files must have unique file names")

    safe_name = prepared_files[0][0]
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

    try:
        with TemporaryDirectory(prefix=f"{Path(safe_name).stem}-", dir=WORKSPACE_ROOT) as directory:
            root = Path(directory).resolve()
            _git(root, "init", "-b", "main")
            _git(root, "config", "user.email", "code-reviewer@localhost")
            _git(root, "config", "user.name", "Code Reviewer")
            _git(root, "commit", "--allow-empty", "-m", "review baseline")
            _git(root, "switch", "-c", "review")

            for upload_name, file_content in prepared_files:
                (root / upload_name).write_bytes(file_content)
            _git(root, "add", "--", ".")
            _git(root, "commit", "-m", "add uploaded review files")
            yield root
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Unable to prepare single-file workspace: {exc}") from exc


def _safe_filename(filename: str | None) -> str:
    name = Path(filename or "uploaded_code.py").name
    name = SAFE_NAME_RE.sub("_", name).strip("._")
    if not name or name.lower() == "git":
        return "uploaded_code.py"
    return name


def _git(root: Path, *args: str) -> None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=500, detail=f"Unable to prepare single-file Git workspace: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise HTTPException(status_code=500, detail=f"Unable to prepare single-file Git workspace: {detail}")
