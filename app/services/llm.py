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
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "category": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "message": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["severity", "category", "line", "message", "suggestion"],
            },
        },
    },
    "required": ["summary", "issues"],
}


class CodeReviewer(ABC):
    @abstractmethod
    async def review(self, language: str, code: str) -> ReviewResponse:
        raise NotImplementedError


class MockReviewer(CodeReviewer):
    async def review(self, language: str, code: str) -> ReviewResponse:
        normalized = re.sub(r"\s+", "", code)
        if language.lower() == "python" and "defadd(" in normalized and "returna-b" in normalized:
            return ReviewResponse(
                summary="函数名与实际行为不一致",
                issues=[
                    {
                        "severity": "high",
                        "category": "logic_bug",
                        "line": 1,
                        "message": "add 函数实际执行了减法",
                        "suggestion": "将 return a-b 改为 return a+b",
                    }
                ],
            )

        issues = []
        if "TODO" in code or "FIXME" in code:
            issues.append(
                {
                    "severity": "low",
                    "category": "maintainability",
                    "line": _find_first_line(code, ("TODO", "FIXME")),
                    "message": "代码中存在待办标记",
                    "suggestion": "将 TODO/FIXME 转换为明确的问题、测试或实现任务。",
                }
            )

        if not issues:
            return ReviewResponse(summary="未发现明显问题", issues=[])
        return ReviewResponse(summary=f"发现 {len(issues)} 个可改进点", issues=issues)


class DeepSeekReviewer(CodeReviewer):
    def __init__(self, settings: Settings):
        if not settings.deepseek_api_key:
            raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY 未配置")
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )

    async def review(self, language: str, code: str) -> ReviewResponse:
        prompt = f"""
请你扮演严谨的代码评审助手，检查下面的 {language} 代码。

只输出 JSON，不要输出 Markdown，不要添加解释性前后缀。
JSON 必须符合这个结构：
{json.dumps(REVIEW_JSON_SCHEMA, ensure_ascii=False)}

评审重点：
1. 逻辑错误
2. 潜在运行时异常
3. 安全问题
4. 可维护性问题
5. 命名与行为是否一致

代码：
```{language}
{code}
```
""".strip()

        try:
            completion = await self.client.chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": "你是一个只返回严格 JSON 的代码评审助手。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"DeepSeek 调用失败: {exc}") from exc

        content = completion.choices[0].message.content
        if not content:
            raise HTTPException(status_code=502, detail="DeepSeek 返回了空内容")

        try:
            data = json.loads(content)
            return ReviewResponse.model_validate(data)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"LLM 返回 JSON 不符合约束: {exc}") from exc


def build_reviewer(settings: Settings) -> CodeReviewer:
    if settings.llm_provider == "deepseek":
        return DeepSeekReviewer(settings)
    return MockReviewer()


def _find_first_line(code: str, keywords: tuple[str, ...]) -> int | None:
    for index, line in enumerate(code.splitlines(), start=1):
        if any(keyword in line for keyword in keywords):
            return index
    return None
