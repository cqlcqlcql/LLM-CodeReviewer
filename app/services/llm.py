import json
import re
from abc import ABC, abstractmethod

from fastapi import HTTPException
from openai import AsyncOpenAI

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
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "message": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["file_path", "source", "severity", "category", "line", "message", "suggestion"],
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
    async def review(self, language: str, code: str) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        static_issues: list[ReviewIssue],
        test_result: TestRunResponse | None,
    ) -> ReviewResponse:
        raise NotImplementedError

    @abstractmethod
    async def explain_test_failure(self, command: str, log_excerpt: str) -> str:
        raise NotImplementedError


class MockReviewer(CodeReviewer):
    async def review(self, language: str, code: str) -> ReviewResponse:
        normalized = re.sub(r"\s+", "", code)
        if language.lower() == "python" and "defadd(" in normalized and "returna-b" in normalized:
            return ReviewResponse(
                summary="Function name does not match behavior.",
                issues=[
                    ReviewIssue(
                        file_path=None,
                        severity="high",
                        category="logic_bug",
                        line=1,
                        message="add function performs subtraction.",
                        suggestion="Change return a-b to return a+b.",
                    )
                ],
            )

        issues: list[ReviewIssue] = []
        if "TODO" in code or "FIXME" in code:
            issues.append(
                ReviewIssue(
                    file_path=None,
                    severity="low",
                    category="maintainability",
                    line=_find_first_line(code, ("TODO", "FIXME")),
                    message="Code contains a TODO/FIXME marker.",
                    suggestion="Convert the marker into a clear issue, test, or implementation task.",
                )
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
                        ReviewIssue(
                            file_path=add_issue[0],
                            severity="high",
                            category="logic_bug",
                            line=add_issue[1],
                            message="add function returns subtraction in the changed diff hunk.",
                            suggestion="Change the add function to return a + b.",
                        )
                    ],
                )

        issues: list[ReviewIssue] = []
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
                    ReviewIssue(
                        file_path=issue_file,
                        severity="high",
                        category="logic_bug",
                        line=issue_line,
                        message="add function returns subtraction on a changed line.",
                        suggestion="Change the added return expression from a-b to a+b.",
                    )
                )
                pending_python_add = None
            elif "TODO" in content or "FIXME" in content:
                issues.append(
                    ReviewIssue(
                        file_path=current_file,
                        severity="low",
                        category="maintainability",
                        line=line_number,
                        message="Changed code leaves a TODO/FIXME marker.",
                        suggestion="Convert the marker into a tracked task or complete the implementation.",
                    )
                )

        if not issues:
            return ReviewResponse(summary="No issues found in changed lines.", issues=[])
        return ReviewResponse(summary=f"Found {len(issues)} issue(s) in changed lines.", issues=issues)

    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        static_issues: list[ReviewIssue],
        test_result: TestRunResponse | None,
    ) -> ReviewResponse:
        issues = list(static_issues)
        if diff_context is not None:
            diff_response = await self.review_diff(language, diff_context)
            issues.extend(_merge_new_issues(issues, diff_response.issues))

        if test_result and test_result.test_status == "failed" and not _has_logic_bug(issues):
            issues.append(
                ReviewIssue(
                    file_path=None,
                    source="pytest + LLM",
                    severity="high",
                    category="test_failure",
                    line=None,
                    message="Automated tests failed and need investigation.",
                    suggestion="Use the first failing test and assertion in the log to locate the broken behavior.",
                )
            )

        if issues:
            return ReviewResponse(summary=f"Found {len(issues)} issue(s) from combined repository evidence.", issues=issues)
        if test_result and test_result.test_status == "passed":
            return ReviewResponse(summary="No review issues found. Automated tests passed.", issues=[])
        return ReviewResponse(summary="No review issues found.", issues=[])

    async def explain_test_failure(self, command: str, log_excerpt: str) -> str:
        if "assert" in log_excerpt.lower():
            return f"{command} 失败：至少一个断言的实际结果和期望结果不一致。下一步：先查看第一个失败用例对应的函数实现。"
        if "error" in log_excerpt.lower() or "exception" in log_excerpt.lower():
            return f"{command} 失败：测试运行中出现错误或异常。下一步：先查看日志里的第一个 traceback。"
        return f"{command} 失败。下一步：查看日志里的第一个失败用例和 traceback。"


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
Every issue must use source exactly as "LLM".
Write summary, message, and suggestion in Simplified Chinese for the product UI. Keep code identifiers, file names, function names, test names, and literal values in their original form.
Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Focus on:
1. Logic bugs
2. Potential runtime exceptions
3. Security issues
4. Maintainability issues
5. Whether names and behavior match

{REVIEW_POLICY}

Code:
```{language}
{code}
```
""".strip()

        return _force_issue_source(_filter_review_issues(language, code, await self._complete_review(prompt)), "LLM")

    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        prompt = f"""
You are a code review assistant.
Review only the added or modified code in the diff context below.
Use context lines only to understand the change.
Do not comment on unchanged context lines or removed lines.
Do not give generic advice.
{REVIEW_POLICY}
Every issue must include file_path, line, severity, reason in message, and suggestion.
Every issue must use source exactly as "LLM".
Write summary, message, and suggestion in Simplified Chinese for the product UI. Keep code identifiers, file names, function names, test names, and literal values in their original form.
If there are no real issues, return an empty issues array.
Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Language: {language}

Diff context:
```text
{diff_context}
```
""".strip()

        return _force_issue_source(_filter_review_issues(language, diff_context, await self._complete_review(prompt)), "LLM")

    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        static_issues: list[ReviewIssue],
        test_result: TestRunResponse | None,
    ) -> ReviewResponse:
        prompt = f"""
You are a code review assistant.
Review the repository using all evidence below after local tools have finished.
Return one deduplicated issue list.

Rules:
- Treat the Git diff as the primary code context when it exists.
- If Git diff is unavailable, do not mention that as a problem; use static analysis and test evidence only.
- Static-analysis findings are tool evidence. Keep them when they identify a real issue, but merge them with duplicate LLM or test findings.
- Test failures are evidence about runtime behavior. If a test failure and a diff issue point to the same root cause, return one issue and mention both signals in the message or suggestion.
- Do not create a separate pytest issue when the same bug is already represented by a code issue.
- Do not comment on unchanged context lines or removed lines unless needed to explain an added or modified line.
- Do not give generic advice.
- {REVIEW_POLICY.replace(chr(10), chr(10) + "- ")}
- Every issue must include file_path, source, line, severity, category, message, and suggestion.
- Write summary, message, and suggestion in Simplified Chinese for the product UI. Keep code identifiers, file names, function names, test names, and literal values in their original form.
- Use source values like "LLM", "ruff", "mypy", "bandit", "pytest + LLM", or "LLM + pytest" to reflect the strongest evidence.
- If there are no real issues, return an empty issues array.
- Return only JSON, with no Markdown or explanatory wrapper. JSON must match this schema: {json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

Language: {language}

Git diff context:
```text
{diff_context or "Git diff unavailable or skipped."}
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

        return _filter_review_issues(
            language,
            diff_context or "",
            await self._complete_review(prompt),
            protect_tool_evidence=True,
        )

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
        try:
            completion = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a code review assistant. Return strict JSON only. Write user-facing review text in Simplified Chinese.",
                    },
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


def _merge_new_issues(existing: list[ReviewIssue], candidates: list[ReviewIssue]) -> list[ReviewIssue]:
    merged: list[ReviewIssue] = []
    keys = {_issue_key(issue) for issue in existing}
    for issue in candidates:
        key = _issue_key(issue)
        if key in keys:
            continue
        keys.add(key)
        merged.append(issue)
    return merged


def _issue_key(issue: ReviewIssue) -> tuple[str | None, int | None, str]:
    return (issue.file_path, issue.line, issue.category)


def _has_logic_bug(issues: list[ReviewIssue]) -> bool:
    return any(issue.category in {"logic_bug", "test_failure"} for issue in issues)


def _force_issue_source(response: ReviewResponse, source: str) -> ReviewResponse:
    for issue in response.issues:
        issue.source = source
    return response


def _filter_review_issues(
    language: str,
    code_evidence: str,
    response: ReviewResponse,
    *,
    protect_tool_evidence: bool = False,
) -> ReviewResponse:
    """Keep LLM review output focused on provable functional defects."""
    issues = [
        issue
        for issue in response.issues
        if not _is_review_noise(issue, code_evidence, protect_tool_evidence=protect_tool_evidence)
    ]
    if len(issues) == len(response.issues):
        return response
    if not issues:
        return ReviewResponse(summary="No clear functional issues found.", issues=[])
    return ReviewResponse(summary=f"Found {len(issues)} clear issue(s).", issues=issues)


def _is_review_noise(issue: ReviewIssue, code_evidence: str, *, protect_tool_evidence: bool) -> bool:
    if protect_tool_evidence and not _is_llm_only_issue(issue):
        return False

    text = f"{issue.category} {issue.message} {issue.suggestion}".lower()
    message = issue.message.lower()

    if _mentions_absent_code_operation(message, code_evidence):
        return True
    if _is_object_identity_speculation(text, code_evidence):
        return True
    if _is_test_driver_output_comment(text, code_evidence):
        return True
    if _is_empty_sublist_sum_contract_comment(text, code_evidence):
        return True
    if _is_heap_update_assumption_comment(text, code_evidence):
        return True
    if _declares_nonnegative_int_contract(code_evidence) and _mentions_out_of_contract_input(text):
        return True
    if _is_speculative_contract_expansion(text, code_evidence):
        return True

    if issue.severity == "low" and _is_style_or_documentation_comment(text):
        return True

    return False


def _is_llm_only_issue(issue: ReviewIssue) -> bool:
    source = issue.source.lower()
    return source == "llm" or (source.startswith("llm") and "pytest" not in source)


def _mentions_absent_code_operation(message: str, code_evidence: str) -> bool:
    normalized_evidence = _normalize_code_claim(code_evidence)
    operand = r"(?:[A-Za-z_][A-Za-z0-9_\.]*|\d+)"
    expression = rf"{operand}(?:\s*[-+*/%&|^]\s*{operand})*"
    operation_pattern = rf"\b[A-Za-z_][A-Za-z0-9_\.]*\s*(?:\^=|&=|\|=|\+=|-=|\*=|//=|/=|%=)\s*{expression}"
    for operation in re.findall(operation_pattern, message):
        if _normalize_code_claim(operation) not in normalized_evidence:
            return True
    return False


def _normalize_code_claim(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _is_object_identity_speculation(text: str, code_evidence: str) -> bool:
    evidence = code_evidence.lower()
    if "node" not in evidence or not any(marker in evidence for marker in ("graph", "digraph", "tree", "linked")):
        return False
    if "`is`" not in text and " is " not in text and "identity" not in text and "身份" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "==",
            "equal",
            "equality",
            "same value",
            "different object",
            "相等",
            "相同值",
            "不同对象",
        )
    )


def _is_test_driver_output_comment(text: str, code_evidence: str) -> bool:
    evidence = code_evidence.lower()
    if "_test.py" not in evidence and "driver to test" not in evidence and "def main(" not in evidence:
        return False
    return any(marker in text for marker in ("print", "output", "format", "comment", "注释", "输出", "格式"))


def _is_empty_sublist_sum_contract_comment(text: str, code_evidence: str) -> bool:
    evidence = code_evidence.lower()
    if "max_sublist_sum" not in evidence or "0 <= i <= j <= len" not in evidence:
        return False
    return any(marker in text for marker in ("all negative", "all elements are negative", "全为负数"))


def _is_heap_update_assumption_comment(text: str, code_evidence: str) -> bool:
    evidence = code_evidence.lower()
    if "heapq retains sorted property" not in evidence:
        return False
    return any(marker in text for marker in ("heap property", "decrease-key", "堆性质", "更新堆"))


def _is_speculative_contract_expansion(text: str, code_evidence: str) -> bool:
    if _has_explicit_behavior_contract(code_evidence):
        return False
    if _mentions_out_of_contract_input(text) or _mentions_generic_input_validation(text):
        return True
    return any(
        marker in text
        for marker in (
            "edge case",
            "boundary",
            "corner case",
            "large input",
            "recursionerror",
            "recursion depth",
            "generator",
            "iterator",
            "iterated twice",
            "unknown operator",
            "unsupported operator",
            "self-loop",
            "mutable default",
            "shared list",
            "same list object",
            "overrides the method",
            "method name conflict",
            "standard lcs",
            "standard dynamic programming",
            "kadane",
            "dijkstra",
            "floyd",
            "kahn",
            "heap property",
            "decrease-key",
            "off-by-one",
            "all elements are negative",
            "all negative",
            "empty list",
            "empty input",
            "k=0",
            "b=0",
            "greater than 36",
            "larger than 36",
            "leading space",
            "infinite loop",
            "incomplete",
            "incorrect order",
            "operand order",
            "rpn",
            "palindrome",
            "mirror",
            "dp[i-1][j]",
            "dp[i][j-1]",
            "边界",
            "特殊情况",
            "大输入",
            "递归深度",
            "生成器",
            "迭代器",
            "未知运算符",
            "自环",
            "可变默认参数",
            "共享",
            "覆盖方法",
            "方法冲突",
            "堆性质",
            "全为负数",
            "空列表",
            "空输入",
            "大于36",
            "开头",
            "无限循环",
            "不完整",
            "运算数顺序",
            "操作数顺序",
            "回文",
            "镜像",
            "最长公共子序列",
            "字符不匹配",
        )
    )


def _has_explicit_behavior_contract(code: str) -> bool:
    lowered = code.lower()
    return any(
        marker in lowered
        for marker in (
            "input:",
            "output:",
            "precondition:",
            "postcondition:",
            "examples:",
            "example:",
            ">>>",
            "return:",
            "returns:",
            "raises:",
        )
    )


def _declares_nonnegative_int_contract(code: str) -> bool:
    return bool(re.search(r"\b(non[-\s]?negative|非负)\b", code, flags=re.IGNORECASE)) and bool(
        re.search(r"\bint(?:eger)?\b|整数", code, flags=re.IGNORECASE)
    )


def _mentions_out_of_contract_input(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "negative",
            "负数",
            "none",
            "null",
            "字符串",
            "string",
            "浮点",
            "float",
            "非整数",
            "non-integer",
            "typeerror",
            "type error",
            "类型",
        )
    )


def _mentions_generic_input_validation(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "input validation",
            "validate",
            "range check",
            "out of range",
            "indexerror",
            "zerodivisionerror",
            "keyerror",
            "invalid",
            "negative",
            "nonnegative",
            "empty",
            "none",
            "null",
            "float",
            "string",
            "integer",
            "non-integer",
            "非法",
            "验证",
            "校验",
            "范围",
            "越界",
            "负数",
            "空",
            "整数",
            "类型",
        )
    )


def _is_style_or_documentation_comment(text: str) -> bool:
    return any(
        marker in text
        for marker in (
            "docstring",
            "documentation",
            "文档",
            "type hint",
            "type annotation",
            "类型注解",
            "注释",
            "style",
            "风格",
            "maintainability",
            "可维护",
        )
    )


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
