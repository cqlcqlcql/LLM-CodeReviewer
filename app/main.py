import re

from fastapi import FastAPI, File, Form, UploadFile
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import ReviewIssue, ReviewRequest, ReviewResponse, TestRunRequest, TestRunResponse
from app.services.code_loader import trim_code
from app.services.diff_loader import load_repository_diff
from app.services.llm import build_reviewer
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
        try:
            diff_context = load_repository_diff(
                payload.repository_path,
                settings.max_code_chars,
                payload.base_branch,
            )
        except HTTPException as exc:
            if not _can_continue_without_diff(exc):
                raise
            response = ReviewResponse(
                summary="No review issues found.",
                issues=[],
                notices=[_diff_unavailable_notice(exc, payload.base_branch)],
            )
        else:
            response = await reviewer.review_diff(payload.language, diff_context)
        if payload.run_static_analysis:
            static_issues = run_static_analysis(payload.repository_path, payload.language)
            response.issues.extend(static_issues)
            response.summary = _append_static_source_summary(response.summary, static_issues)
        if payload.run_tests:
            response.test_result = await run_project_tests(
                payload.repository_path,
                payload.language,
                60,
                reviewer,
            )
            response.summary = _append_test_result_summary(response.summary, response.test_result)
        return response

    code = payload.code
    assert code is not None

    return await reviewer.review(payload.language, trim_code(code, settings.max_code_chars))


@app.post("/api/review/file", response_model=ReviewResponse)
async def review_file(
    language: str = Form(default="python"),
    file: UploadFile = File(...),
) -> ReviewResponse:
    settings = get_settings()
    raw = await file.read()
    code = raw.decode("utf-8", errors="ignore")
    reviewer = build_reviewer(settings)
    return await reviewer.review(language, trim_code(code, settings.max_code_chars))


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


def _append_static_source_summary(summary: str, issues: list[ReviewIssue]) -> str:
    if not issues:
        return summary

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue.source] = counts.get(issue.source, 0) + 1
    source_summary = ", ".join(f"{source}: {count}" for source, count in sorted(counts.items()))
    return f"{summary} Static sources: {source_summary}."


def _append_test_result_summary(summary: str, test_result: TestRunResponse) -> str:
    if test_result.test_status == "passed":
        test_summary = "Automated tests passed."
    elif test_result.test_status == "failed":
        if test_result.failed_cases is None:
            test_summary = "Automated tests failed."
        else:
            test_summary = f"Automated tests failed with {test_result.failed_cases} failing case(s)."
        cause = _first_test_explanation_sentence(test_result.llm_explanation)
        if cause:
            test_summary = f"{test_summary} {cause}"
    elif test_result.test_status == "timeout":
        test_summary = "Automated tests timed out."
    elif test_result.test_status == "unsupported":
        test_summary = "No supported automated test command was detected."
    else:
        test_summary = "Automated tests could not be started."

    if summary == "No review issues found." or summary.startswith("No review issues found. Static sources:"):
        return test_summary
    return f"{summary} {test_summary}"


def _first_test_explanation_sentence(explanation: str | None) -> str:
    if not explanation:
        return ""
    cleaned = re.sub(r"[*_`#>-]+", "", explanation)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return ""
    match = re.match(r"(.+?[.!?])(?:\s|$)", cleaned)
    sentence = match.group(1) if match else cleaned
    if len(sentence) > 220:
        sentence = sentence[:217].rstrip() + "..."
    return sentence


def _is_no_diff_error(exc: HTTPException) -> bool:
    return exc.status_code == 400 and isinstance(exc.detail, str) and exc.detail.startswith("No diff found for ")


def _is_non_git_repository_error(exc: HTTPException) -> bool:
    return exc.status_code == 400 and isinstance(exc.detail, str) and exc.detail.startswith("Path is not a Git repository:")


def _can_continue_without_diff(exc: HTTPException) -> bool:
    return _is_no_diff_error(exc) or _is_non_git_repository_error(exc)


def _diff_unavailable_notice(exc: HTTPException, base_branch: str) -> str:
    if _is_no_diff_error(exc):
        return f"No diff found for {base_branch}...HEAD; skipped Git diff review."
    return "Path is not a Git repository; skipped Git diff review."
