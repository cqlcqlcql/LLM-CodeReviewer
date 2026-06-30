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

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}


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
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="utf-8", errors="ignore")

        relative = path.relative_to(root)
        block = f"\n\n# File: {relative}\n{content}"
        chunks.append(block)
        total += len(block)
        if total >= max_chars:
            break

    if not chunks:
        raise HTTPException(status_code=400, detail="没有找到可评审的代码文件")

    return trim_code("".join(chunks).strip(), max_chars)
