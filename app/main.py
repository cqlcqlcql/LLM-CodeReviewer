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
            if not _is_no_diff_error(exc):
                raise
            response = ReviewResponse(summary=f"No diff found for {payload.base_branch}...HEAD.", issues=[])
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


def _is_no_diff_error(exc: HTTPException) -> bool:
    return exc.status_code == 400 and isinstance(exc.detail, str) and exc.detail.startswith("No diff found for ")
