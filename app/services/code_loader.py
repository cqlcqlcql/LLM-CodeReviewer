import os
from pathlib import Path

from fastapi import HTTPException

LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".jsx"},
    "typescript": {".ts", ".tsx"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".h"},
    "java": {".java"},
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "tests",
    "test",
}

MAX_SOURCE_FILE_BYTES = 1_000_000


def trim_code(code: str, max_chars: int) -> str:
    if len(code) <= max_chars:
        return code
    return code[:max_chars] + "\n\n# ... code truncated for review ..."


def load_repository_code(repository_path: str, language: str, max_chars: int) -> str:
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在: {repository_path}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {repository_path}")

    extensions = LANGUAGE_EXTENSIONS.get(language.lower())
    if extensions is None:
        extensions = {".py", ".js", ".ts", ".c", ".cpp", ".java"}

    chunks: list[str] = []
    total = 0
    for current_root, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(name for name in directory_names if name not in SKIP_DIRS)
        for file_name in sorted(file_names):
            path = Path(current_root) / file_name
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            if path.suffix.lower() not in extensions:
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                    continue
            except OSError:
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            relative = path.relative_to(root)
            block = f"\n\n# File: {relative}\n{content}"
            chunks.append(block)
            total += len(block)
            if total >= max_chars:
                break
        if total >= max_chars:
            break

    if not chunks:
        raise HTTPException(status_code=400, detail="没有找到可评审的代码文件")

    return trim_code("".join(chunks).strip(), max_chars)
