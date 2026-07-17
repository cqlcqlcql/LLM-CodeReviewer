import re

import pytest

import app.main as main_module
from app.schemas import ReviewIssue, ReviewResponse


class DeterministicTestReviewer:
    """Network-free reviewer used only by the automated test suite."""

    async def review(self, language: str, code: str) -> ReviewResponse:
        issue = self._find_add_subtract(code)
        if issue:
            return ReviewResponse(summary="Function name does not match behavior.", issues=[issue])
        return ReviewResponse(summary="No review issues found.", issues=[])

    async def review_diff(self, language: str, diff_context: str) -> ReviewResponse:
        issue = self._find_add_subtract(diff_context)
        if issue:
            issue.file_path = self._diff_file(diff_context)
            issue.line = self._diff_line(diff_context)
            return ReviewResponse(summary="Found 1 issue(s) in changed lines.", issues=[issue])

        issues: list[ReviewIssue] = []
        if "TODO" in diff_context or "FIXME" in diff_context:
            issues.append(
                ReviewIssue(
                    file_path=self._diff_file(diff_context),
                    source="LLM",
                    severity="low",
                    category="maintainability",
                    line=self._diff_line(diff_context),
                    message="Changed code leaves a TODO/FIXME marker.",
                    suggestion="Complete the implementation or track the remaining work.",
                )
            )
        return ReviewResponse(summary=f"Found {len(issues)} issue(s) in changed lines.", issues=issues)

    async def review_repository(
        self,
        language: str,
        diff_context: str | None,
        static_issues: list[ReviewIssue],
        test_result,
    ) -> ReviewResponse:
        issues = list(static_issues)
        if diff_context:
            diff_response = await self.review_diff(language, diff_context)
            issues.extend(diff_response.issues)
        if test_result and test_result.test_status == "failed" and not any(
            issue.category in {"logic_bug", "test_failure"} for issue in issues
        ):
            issues.append(
                ReviewIssue(
                    source="pytest + LLM",
                    severity="high",
                    category="test_failure",
                    message="Automated tests failed and need investigation.",
                    suggestion="Inspect the first failed test and assertion.",
                )
            )
        if issues:
            return ReviewResponse(
                summary=f"Found {len(issues)} issue(s) from combined repository evidence.",
                issues=issues,
            )
        if test_result and test_result.test_status == "passed":
            return ReviewResponse(summary="No review issues found. Automated tests passed.", issues=[])
        return ReviewResponse(summary="No review issues found.", issues=[])

    async def explain_test_failure(self, command: str, log_excerpt: str) -> str:
        return f"结论：{command} 存在失败用例，请先检查第一个断言。"

    @staticmethod
    def _find_add_subtract(code: str) -> ReviewIssue | None:
        normalized = re.sub(r"\s+", "", code)
        if "defadd(" not in normalized or "returna-b" not in normalized:
            return None
        return ReviewIssue(
            source="LLM",
            severity="high",
            category="logic_bug",
            line=1,
            message="add function performs subtraction.",
            suggestion="Change return a-b to return a+b.",
        )

    @staticmethod
    def _diff_file(diff_context: str) -> str | None:
        match = re.search(r"^FILE:\s+(.+)$", diff_context, flags=re.MULTILINE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _diff_line(diff_context: str) -> int | None:
        match = re.search(r"ADDED new_line=(\d+):\s+def\s+add", diff_context)
        if not match:
            match = re.search(r"ADDED new_line=(\d+):", diff_context)
        return int(match.group(1)) if match else None


@pytest.fixture(autouse=True)
def use_test_reviewer(monkeypatch):
    monkeypatch.setattr(main_module, "build_reviewer", lambda settings: DeterministicTestReviewer())
