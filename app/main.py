import re

from fastapi import FastAPI, File, Form, UploadFile
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    ChangedFileSummary,
    DiffRequest,
    DiffResponse,
    ReviewIssue,
    ReviewRequest,
    ReviewResponse,
    TestRunRequest,
    TestRunResponse,
)
from app.services.code_loader import load_repository_code, trim_code
from app.services.diff_loader import load_repository_diff, load_repository_unified_diff, parse_unified_diff
from app.services.llm import _filter_review_issues, _has_logic_bug, _merge_new_issues, build_reviewer
from app.services.static_analysis import run_static_analysis
from app.services.test_runner import run_project_tests
from app.settings import get_settings

app = FastAPI(title="Code Review MVP", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("frontend/index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "provider": settings.llm_provider}


@app.post("/api/review", response_model=ReviewResponse)
async def review_code(payload: ReviewRequest) -> ReviewResponse:
    settings = get_settings()
    reviewer = build_reviewer(settings)
    if payload.repository_path:
        diff_context: str | None = None
        source_context: str | None = None
        notices: list[str] = []
        static_issues: list[ReviewIssue] = []
        test_result: TestRunResponse | None = None

        try:
            diff_context = load_repository_diff(
                payload.repository_path,
                settings.max_code_chars,
                payload.base_branch,
            )
        except HTTPException as exc:
            if not _can_continue_without_diff(exc):
                raise
            notices.append(_diff_unavailable_notice(exc, payload.base_branch))
            if _is_non_git_repository_error(exc):
                try:
                    source_context = load_repository_code(
                        payload.repository_path,
                        payload.language,
                        settings.max_code_chars,
                    )
                except HTTPException as code_exc:
                    if not _is_no_reviewable_code_error(code_exc):
                        raise

        if payload.run_static_analysis:
            static_issues = run_static_analysis(payload.repository_path, payload.language)

        if payload.run_tests:
            test_result = await run_project_tests(
                payload.repository_path,
                payload.language,
                60,
                reviewer,
                explain_failures=False,
            )

        if source_context is not None:
            response = await reviewer.review(payload.language, source_context)
            if static_issues:
                response.issues.extend(_merge_new_issues(response.issues, static_issues))
            if test_result and test_result.test_status == "failed" and not _has_logic_bug(response.issues):
                response.issues.append(
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
            response = _summarize_repository_response(response, test_result)
            response = _filter_review_issues(
                payload.language,
                source_context,
                response,
                protect_tool_evidence=True,
            )
        elif diff_context is not None or static_issues or test_result is not None:
            response = await reviewer.review_repository(
                payload.language,
                diff_context,
                static_issues,
                test_result,
            )
            response = _filter_review_issues(
                payload.language,
                diff_context or "",
                response,
                protect_tool_evidence=True,
            )
        else:
            response = ReviewResponse(
                summary="No review issues found.",
                issues=[],
            )

        response.notices.extend(notices)
        if test_result is not None:
            response.test_result = test_result
        return response

    code = payload.code
    assert code is not None

    trimmed_code = trim_code(code, settings.max_code_chars)
    response = await reviewer.review(payload.language, trimmed_code)
    response = _filter_review_issues(payload.language, trimmed_code, response)
    return _force_direct_review_source(response)


@app.post("/api/review/file", response_model=ReviewResponse)
async def review_file(
    language: str = Form(default="python"),
    file: UploadFile = File(...),
) -> ReviewResponse:
    settings = get_settings()
    raw = await file.read()
    code = raw.decode("utf-8", errors="ignore")
    reviewer = build_reviewer(settings)
    trimmed_code = trim_code(code, settings.max_code_chars)
    response = await reviewer.review(language, trimmed_code)
    response = _filter_review_issues(language, trimmed_code, response)
    return _force_direct_review_source(response)


@app.post("/api/test", response_model=TestRunResponse)
async def test_project(payload: TestRunRequest) -> TestRunResponse:
    settings = get_settings()
    reviewer = build_reviewer(settings)
    return await run_project_tests(
        payload.repository_path,
        payload.language,
        payload.timeout_seconds,
        reviewer,
    )


@app.post("/api/diff", response_model=DiffResponse)
async def repository_diff(payload: DiffRequest) -> DiffResponse:
    diff = load_repository_unified_diff(payload.repository_path, payload.base_branch)
    files = [
        ChangedFileSummary(
            path=changed_file.path,
            additions=sum(1 for hunk in changed_file.hunks for line in hunk.lines if line.kind == "+"),
            deletions=sum(1 for hunk in changed_file.hunks for line in hunk.lines if line.kind == "-"),
            hunks=len(changed_file.hunks),
        )
        for changed_file in parse_unified_diff(diff)
    ]
    return DiffResponse(base_branch=payload.base_branch, diff=diff, files=files)


def _is_no_diff_error(exc: HTTPException) -> bool:
    return exc.status_code == 400 and isinstance(exc.detail, str) and exc.detail.startswith("No diff found for ")


def _is_non_git_repository_error(exc: HTTPException) -> bool:
    return exc.status_code == 400 and isinstance(exc.detail, str) and exc.detail.startswith("Path is not a Git repository:")


def _can_continue_without_diff(exc: HTTPException) -> bool:
    return _is_no_diff_error(exc) or _is_non_git_repository_error(exc)


def _is_no_reviewable_code_error(exc: HTTPException) -> bool:
    return exc.status_code == 400


def _diff_unavailable_notice(exc: HTTPException, base_branch: str) -> str:
    if _is_no_diff_error(exc):
        return f"No diff found for {base_branch}...HEAD; skipped Git diff review."
    return "Path is not a Git repository; skipped Git diff review."


def _force_direct_review_source(response: ReviewResponse) -> ReviewResponse:
    for issue in response.issues:
        issue.source = "LLM"
    return response


def _summarize_repository_response(response: ReviewResponse, test_result: TestRunResponse | None) -> ReviewResponse:
    if response.issues:
        response.summary = f"Found {len(response.issues)} issue(s) from combined repository evidence."
    elif test_result and test_result.test_status == "passed":
        response.summary = "No review issues found. Automated tests passed."
    else:
        response.summary = "No review issues found."
    return response
