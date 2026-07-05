import json
import re
from abc import ABC, abstractmethod

from fastapi import HTTPException
from openai import AsyncOpenAI

from app.schemas import ReviewResponse
from app.settings import Settings


REVIEW_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file_path": {"type": ["string", "null"]},
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "message": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file_path", "severity", "category", "line", "message", "suggestion"],
            },
        },
    },
    "required": ["summary", "issues"],
}


class CodeReviewer(ABC):
    @abstractmethod
    async def review(self, language: str, code: str) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        raise NotImplementedError


class MockReviewer(CodeReviewer):
    async def review(self, language: str, code: str) -> ReviewResponse:
        normalized = re.sub(r"\s+", "", code)
        if language.lower() == "python" and "defadd(" in normalized and "returna-b" in normalized:
            return ReviewResponse(
                summary="Function name does not match behavior.",
                issues=[
                    {
                        "file_path": None,
                        "severity": "high",
                        "category": "logic_bug",
                        "line": 1,
                        "message": "add function performs subtraction.",
                        "suggestion": "Change return a-b to return a+b.",
                    }
                ],
            )

        issues = []
        if "TODO" in code or "FIXME" in code:
            issues.append(
                {
                    "file_path": None,
                    "severity": "low",
                    "category": "maintainability",
                    "line": _find_first_line(code, ("TODO", "FIXME")),
                    "message": "Code contains a TODO/FIXME marker.",
                    "suggestion": "Convert the marker into a clear issue, test, or implementation task.",
                }
            )

        if not issues:
            return ReviewResponse(summary="No obvious issues found.", issues=[])
        return ReviewResponse(summary=f"Found {len(issues)} issue(s).", issues=issues)

    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        if language.lower() == "python":
            add_issue = _find_added_python_add_subtract_issue(diff_context)
            if add_issue is not None:
                return ReviewResponse(
                    summary="Found 1 issue(s) in changed lines.",
                    issues=[
                        {
                            "file_path": add_issue[0],
                            "severity": "high",
                            "category": "logic_bug",
                            "line": add_issue[1],
                            "message": "add function returns subtraction in the changed diff hunk.",
                            "suggestion": "Change the add function to return a + b.",
                        }
                    ],
                )

        issues = []
        current_file: str | None = None
        pending_python_add: tuple[str | None, int | None] | None = None
        for line in diff_context.splitlines():
            if line.startswith("FILE: "):
                current_file = line.removeprefix("FILE: ").strip()
                pending_python_add = None
                continue

            is_added = line.startswith("ADDED ")
            is_context = line.startswith("CONTEXT ")
            if not is_added and not is_context:
                continue

            line_number = _extract_new_line_number(line)
            content = line.split(": ", 1)[1] if ": " in line else line
            normalized = re.sub(r"\s+", "", content)

            if language.lower() == "python" and is_added and "defadd(" in normalized:
                pending_python_add = (current_file, line_number)

            if language.lower() == "python" and (
                is_added and "defadd(" in normalized and "returna-b" in normalized
                or pending_python_add is not None and "returna-b" in normalized
            ):
                issue_file, issue_line = pending_python_add or (current_file, line_number)
                issues.append(
                    {
                        "file_path": issue_file,
                        "severity": "high",
                        "category": "logic_bug",
                        "line": issue_line,
                        "message": "add function returns subtraction on a changed line.",
                        "suggestion": "Change the added return expression from a-b to a+b.",
                    }
                )
                pending_python_add = None
            elif "TODO" in content or "FIXME" in content:
                issues.append(
                    {
                        "file_path": current_file,
                        "severity": "low",
                        "category": "maintainability",
                        "line": line_number,
                        "message": "Changed code leaves a TODO/FIXME marker.",
                        "suggestion": "Convert the marker into a tracked task or complete the implementation.",
                    }
                )

        if not issues:
            return ReviewResponse(summary="No issues found in changed lines.", issues=[])
        return ReviewResponse(summary=f"Found {len(issues)} issue(s) in changed lines.", issues=issues)


class DeepSeekReviewer(CodeReviewer):
    def __init__(self, settings: Settings):
        if not settings.deepseek_api_key:
            raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not configured")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

    async def review(self, language: str, code: str) -> ReviewResponse:
        prompt = f"""
Please act as a careful code review assistant and inspect the following {language} code.
Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Focus on:
1. Logic bugs
2. Potential runtime exceptions
3. Security issues
4. Maintainability issues
5. Whether names and behavior match

Code:
```{language}
{code}
```
""".strip()

        return await self._complete_review(prompt)

    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        prompt = f"""
You are a code review assistant.
Review only the added or modified code in the diff context below.
Use context lines only to understand the change.
Do not comment on unchanged context lines or removed lines.
Do not give generic advice.
Every issue must include file_path, line, severity, reason in message, and suggestion.
If there are no real issues, return an empty issues array.
Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Language: {language}

Diff context:
```text
{diff_context}
```
""".strip()

        return await self._complete_review(prompt)

    async def _complete_review(self, prompt: str) -> ReviewResponse:
        try:
            completion = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": "You are a code review assistant that returns strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DeepSeek request failed: {exc}") from exc

        content = completion.choices[0].message.content
        if not content:
            raise HTTPException(status_code=502, detail="DeepSeek returned empty content")

        try:
            data = json.loads(content)
            return ReviewResponse.model_validate(data)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM JSON did not match the contract: {exc}") from exc


def build_reviewer(settings: Settings) -> CodeReviewer:
    if settings.llm_provider == "deepseek":
        return DeepSeekReviewer(settings)
    return MockReviewer()


def _find_first_line(code: str, keywords: tuple[str, ...]) -> int | None:
    for index, line in enumerate(code.splitlines(), start=1):
        if any(keyword in line for keyword in keywords):
            return index
    return None


def _extract_new_line_number(line: str) -> int | None:
    match = re.search(r"new_line=(\d+)", line)
    if not match:
        return None
    return int(match.group(1))


def _find_added_python_add_subtract_issue(diff_context: str) -> tuple[str | None, int] | None:
    current_file: str | None = None
    hunk_lines: list[str] = []

    for line in [*diff_context.splitlines(), "FILE: __end__"]:
        if line.startswith("FILE: "):
            issue = _find_issue_in_hunk(current_file, hunk_lines)
            if issue is not None:
                return issue
            current_file = line.removeprefix("FILE: ").strip()
            hunk_lines = []
        else:
            hunk_lines.append(line)

    return None


def _find_issue_in_hunk(file_path: str | None, hunk_lines: list[str]) -> tuple[str | None, int] | None:
    for index, line in enumerate(hunk_lines):
        if not line.startswith("ADDED "):
            continue

        content = line.split(": ", 1)[1] if ": " in line else line
        if "defadd(" not in re.sub(r"\s+", "", content):
            continue

        line_number = _extract_new_line_number(line)
        if line_number is None:
            continue

        following = "\n".join(hunk_lines[index : index + 8])
        if "returna-b" in re.sub(r"\s+", "", following):
            return file_path, line_number

    return None
