import json
from abc import ABC, abstractmethod

from fastapi import HTTPException
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.schemas import ReviewIssue, ReviewResponse, TestRunResponse
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
                    "source": {"type": "string"},
                    "evidence_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "message": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file_path", "evidence_sources", "severity", "category", "line", "message", "suggestion"],
            },
        },
    },
    "required": ["summary", "issues"],
}

REVIEW_POLICY = """Review policy:
- Report only issues that are demonstrably wrong for the code's stated purpose or explicit input contract.
- Respect constraints documented in comments or docstrings, such as "nonnegative int"; do not require extra validation for inputs outside that contract.
- Do not report missing docstrings, type hints, style preferences, or generic maintainability advice for small standalone snippets.
- Be conservative with well-known idioms and exact code claims; do not report an operation, branch, call, or literal that is not present in the reviewed code or diff evidence.
- For graph, tree, or linked-structure algorithms, treat nodes as object identities unless the contract explicitly says value equality is required.
- Prefer an empty issues array over speculative findings."""


class CodeReviewer(ABC):
    @abstractmethod
    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        source_context: str | None,
        static_issues: list[ReviewIssue],
        test_result: TestRunResponse | None,
    ) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def explain_test_failure(self, command: str, log_excerpt: str) -> str:
        raise NotImplementedError


class DeepSeekReviewer(CodeReviewer):
    def __init__(self, settings: Settings):
        if not settings.deepseek_api_key:
            raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not configured")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        prompt = f"""
You are a code review assistant.
Review only the added or modified code in the diff context below.
Use context lines only to understand the change.
Do not comment on unchanged context lines or removed lines.
Do not give generic advice.
{REVIEW_POLICY}
Every issue must include file_path, line, severity, reason in message, and suggestion.
Every issue must use evidence_sources exactly as ["LLM"].
Write summary, message, and suggestion in Simplified Chinese for the product UI. Keep code identifiers, file names, function names, test names, and literal values in their original form.
If there are no real issues, return an empty issues array.
Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Language: {language}

Diff context:
```text
{diff_context}
```
""".strip()

        return _force_issue_source(await self._complete_review(prompt), "LLM")

    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        source_context: str | None,
        static_issues: list[ReviewIssue],
        test_result: TestRunResponse | None,
    ) -> ReviewResponse:
        prompt = f"""
You are a code review assistant.
Review the repository using all evidence below after local tools have finished.
Return one deduplicated issue list.

Rules:
- Treat the Git diff as the primary code context when it exists.
- If Git diff is unavailable, review the bounded source snapshot when provided.
- If both Git diff and source snapshot are unavailable, use static analysis and test evidence only.
- Static-analysis findings are tool evidence. Keep them when they identify a real issue, but merge them with duplicate LLM or test findings.
- Test failures are evidence about runtime behavior. If a test failure and a diff issue point to the same root cause, return one issue and mention both signals in the message or suggestion.
- Do not create a separate pytest issue when the same bug is already represented by a code issue.
- Do not comment on unchanged context lines or removed lines unless needed to explain an added or modified line.
- Do not give generic advice.
- {REVIEW_POLICY.replace(chr(10), chr(10) + "- ")}
- Every issue must include file_path, evidence_sources, line, severity, category, message, and suggestion.
- Write summary, message, and suggestion in Simplified Chinese for the product UI. Keep code identifiers, file names, function names, test names, and literal values in their original form.
- Use evidence_sources arrays like ["LLM"], ["ruff"], or ["pytest", "LLM"] to reflect all evidence supporting each issue.
- If there are no real issues, return an empty issues array.
- Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Language: {language}

Git diff context:
```text
{diff_context or "Git diff unavailable or skipped."}
```

Bounded source snapshot:
```{language}
{source_context or "Source snapshot unavailable or disabled."}
```

Static analysis issues:
```json
{json.dumps([issue.model_dump() for issue in static_issues], ensure_ascii=False, indent=2)}
```

Test result:
```json
{json.dumps(test_result.model_dump() if test_result else None, ensure_ascii=False, indent=2)}
```
""".strip()

        return await self._complete_review(prompt)

    async def explain_test_failure(self, command: str, log_excerpt: str) -> str:
        prompt = f"""
你正在解释一次失败的自动化测试。
请用中文输出，面向普通开发者，必须短、清楚、可执行。
不要编造日志里没有出现的文件、函数或用例。

输出格式固定如下：
结论：一句话说明这次失败的核心原因。

| 用例 | 可能原因 | 下一步 |
| --- | --- | --- |
| test_name | 不超过 30 个中文字 | 不超过 30 个中文字 |

最多列出 5 个最重要的失败用例。不要输出 Markdown 表格以外的长段解释。

Command:
{command}

Test log:
```text
{log_excerpt}
```
""".strip()

        try:
            completion = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": "你用简洁中文解释测试失败，并按固定表格输出。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DeepSeek request failed: {exc}") from exc

        content = completion.choices[0].message.content
        if not content:
            raise HTTPException(status_code=502, detail="DeepSeek returned empty content")
        return content.strip()

    async def _complete_review(self, prompt: str) -> ReviewResponse:
        messages: list[ChatCompletionMessageParam] = [
            {
                "role": "system",
                "content": "You are a code review assistant. Return strict JSON only. Write user-facing review text in Simplified Chinese.",
            },
            {"role": "user", "content": prompt},
        ]
        content = await self._request_review_completion(messages)

        try:
            return _parse_review_response(content)
        except Exception as first_error:
            repair_messages: list[ChatCompletionMessageParam] = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON for the required schema. "
                        f"The parser reported: {first_error}. "
                        "Correct the syntax and schema now. Preserve the intended review findings, "
                        "escape quotes and newlines correctly, and return only one strict JSON object."
                    ),
                },
            ]
            repaired_content = await self._request_review_completion(repair_messages)
            try:
                return _parse_review_response(repaired_content)
            except Exception as second_error:
                raise HTTPException(
                    status_code=502,
                    detail=f"DeepSeek returned invalid structured JSON after one retry: {second_error}",
                ) from second_error

    async def _request_review_completion(
        self, messages: list[ChatCompletionMessageParam]
    ) -> str:
        try:
            completion = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DeepSeek request failed: {exc}") from exc

        content = completion.choices[0].message.content
        if not content:
            raise HTTPException(status_code=502, detail="DeepSeek returned empty content")
        return content.strip()


def build_reviewer(settings: Settings) -> CodeReviewer:
    return DeepSeekReviewer(settings)


def _force_issue_source(response: ReviewResponse, source: str) -> ReviewResponse:
    for issue in response.issues:
        issue.evidence_sources = [source]
        issue.source = source
    return response


def _parse_review_response(content: str) -> ReviewResponse:
    data = json.loads(content)
    return ReviewResponse.model_validate(data)
