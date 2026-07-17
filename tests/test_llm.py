import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.llm import DeepSeekReviewer
from app.settings import Settings


def _completion(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def test_deepseek_retries_invalid_review_json_once():
    reviewer = DeepSeekReviewer(Settings(DEEPSEEK_API_KEY="test-key"))
    create = AsyncMock(
        side_effect=[
            _completion('{"summary": "bad" "issues": []}'),
            _completion('{"summary": "已修正", "issues": []}'),
        ]
    )
    reviewer.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    response = asyncio.run(reviewer._complete_review("Review this code"))

    assert response.summary == "已修正"
    assert response.issues == []
    assert create.await_count == 2
    repair_messages = create.await_args_list[1].kwargs["messages"]
    assert "previous response was not valid JSON" in repair_messages[-1]["content"]

