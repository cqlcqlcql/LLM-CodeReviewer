import os
import re
from dataclasses import dataclass
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
    ".agents",
    ".codex",
    ".idea",
    ".vscode",
    ".tmp",
    "dist",
    "build",
    "tests",
    "test",
}

MAX_SOURCE_FILE_BYTES = 1_000_000
SENSITIVE_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.json",
}
SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".crt", ".cer"}


@dataclass(frozen=True)
class RepositorySourceContext:
    content: str
    files: tuple[str, ...]
    chars: int
    truncated: bool


def trim_code(code: str, max_chars: int) -> str:
    if len(code) <= max_chars:
        return code
    return code[:max_chars] + "\n\n# ... code truncated for review ..."


def load_repository_source_context(
    repository_path: str,
    language: str,
    max_chars: int,
    preferred_paths: set[str] | None = None,
) -> RepositorySourceContext:
    root = Path(repository_path).expanduser().resolve()
    if not root.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在: {repository_path}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"路径不是目录: {repository_path}")

    extensions = LANGUAGE_EXTENSIONS.get(language.lower())
    if extensions is None:
        extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".h", ".cpp", ".java"}

    candidates: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root):
        directory_names[:] = sorted(name for name in directory_names if name not in SKIP_DIRS)
        for file_name in sorted(file_names):
            path = Path(current_root) / file_name
            if path.name.startswith("test_") or path.name.endswith("_test.py"):
                continue
            if path.name.lower() in SENSITIVE_FILE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES:
                continue
            if path.suffix.lower() not in extensions:
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_FILE_BYTES:
                    continue
            except OSError:
                continue
            candidates.append(path)

    preferred = {_normalize_relative_path(value) for value in preferred_paths or set()}
    candidates.sort(
        key=lambda path: (
            not _is_preferred_path(_normalize_relative_path(str(path.relative_to(root))), preferred),
            _normalize_relative_path(str(path.relative_to(root))),
        )
    )

    chunks: list[str] = []
    included_files: list[str] = []
    total = 0
    truncated = False
    for path in candidates:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        relative = path.relative_to(root)
        block = f"\n\n# File: {relative}\n{content}"
        remaining = max_chars - total
        if remaining <= 0:
            truncated = True
            break
        if len(block) > remaining:
            if chunks:
                truncated = True
                break
            block = block[:remaining]
            truncated = True
        chunks.append(block)
        included_files.append(str(relative).replace("\\", "/"))
        total += len(block)
        if truncated:
            break

    if len(included_files) < len(candidates):
        truncated = True
    if not chunks:
        raise HTTPException(status_code=400, detail="没有找到可评审的源码文件")

    joined = "".join(chunks).strip()
    return RepositorySourceContext(
        content=joined,
        files=tuple(included_files),
        chars=len(joined),
        truncated=truncated,
    )


def load_repository_code(repository_path: str, language: str, max_chars: int) -> str:
    return load_repository_source_context(repository_path, language, max_chars).content


def extract_referenced_source_paths(text: str) -> set[str]:
    matches = re.findall(
        r"(?<![\w.-])([\w./\\-]+\.(?:py|js|jsx|ts|tsx|c|h|cc|cpp|cxx|hpp|java))(?::\d+)?",
        text,
        flags=re.IGNORECASE,
    )
    return {_normalize_relative_path(match) for match in matches}


def _normalize_relative_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _is_preferred_path(relative_path: str, preferred_paths: set[str]) -> bool:
    return any(
        value == relative_path or value.endswith(f"/{relative_path}")
        for value in preferred_paths
    )
