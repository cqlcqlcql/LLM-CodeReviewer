from fastapi import FastAPI, File, Form, UploadFile
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import (
    ChangedFileSummary,
    CodeContext,
    DiffRequest,
    DiffResponse,
    ReviewIssue,
    ReviewRequest,
    ReviewResponse,
    TestRunRequest,
    TestRunResponse,
)
from app.services.code_loader import (
    RepositorySourceContext,
    extract_referenced_source_paths,
    load_repository_source_context,
)
from app.services.diff_loader import load_repository_diff, load_repository_unified_diff, parse_unified_diff
from app.services.llm import CodeReviewer, build_reviewer
from app.services.single_file_workspace import single_file_repository
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
async def health() -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "status": "ok",
        "provider": "deepseek",
        "provider_configured": bool(settings.deepseek_api_key),
    }


@app.post("/api/review", response_model=ReviewResponse)
async def review_repository(payload: ReviewRequest) -> ReviewResponse:
    settings = get_settings()
    reviewer = build_reviewer(settings)
    return await _review_repository_path(
        repository_path=payload.repository_path,
        language=payload.language,
        base_branch=payload.base_branch,
        static_analysis_enabled=payload.run_static_analysis,
        tests_enabled=payload.run_tests,
        include_source_context=payload.include_source_context,
        reviewer=reviewer,
        max_code_chars=settings.max_code_chars,
    )


@app.post("/api/review/file", response_model=ReviewResponse)
async def review_file(
    language: str = Form(default="python"),
    run_static_analysis: bool = Form(default=True),
    run_tests: bool = Form(default=True),
    file: UploadFile = File(...),
    related_files: list[UploadFile] | None = File(default=None),
) -> ReviewResponse:
    settings = get_settings()
    raw = await file.read()
    related_contents = [
        (related_file.filename, await related_file.read()) for related_file in related_files or []
    ]
    reviewer = build_reviewer(settings)
    with single_file_repository(file.filename, raw, related_contents) as repository_path:
        response = await _review_repository_path(
            repository_path=str(repository_path),
            language=language,
            base_branch="main",
            static_analysis_enabled=run_static_analysis,
            tests_enabled=run_tests,
            include_source_context=True,
            reviewer=reviewer,
            max_code_chars=settings.max_code_chars,
        )
    response.notices.insert(
        0,
        f"Uploaded source and {len(related_contents)} related file(s) were reviewed in an isolated temporary Git workspace.",
    )
    return response


async def _review_repository_path(
    *,
    repository_path: str,
    language: str,
    base_branch: str,
    static_analysis_enabled: bool,
    tests_enabled: bool,
    include_source_context: bool,
    reviewer: CodeReviewer,
    max_code_chars: int,
) -> ReviewResponse:
    diff_context: str | None = None
    notices: list[str] = []
    static_issues: list[ReviewIssue] = []
    test_result: TestRunResponse | None = None
    source_snapshot: RepositorySourceContext | None = None
    non_git_directory = False

    try:
        diff_context = load_repository_diff(repository_path, max_code_chars, base_branch)
    except HTTPException as exc:
        if not _can_continue_without_diff(exc):
            raise
        notices.append(_diff_unavailable_notice(exc, base_branch))
        if _is_non_git_repository_error(exc):
            non_git_directory = True

    if static_analysis_enabled:
        static_issues = run_static_analysis(repository_path, language)

    if tests_enabled:
        test_result = await run_project_tests(
            repository_path,
            language,
            60,
            reviewer,
            explain_failures=False,
        )

    if non_git_directory and include_source_context:
        preferred_paths = {
            issue.file_path for issue in static_issues if issue.file_path is not None
        }
        if test_result is not None:
            preferred_paths.update(extract_referenced_source_paths(test_result.log_excerpt))
        try:
            source_snapshot = load_repository_source_context(
                repository_path,
                language,
                max_code_chars,
                preferred_paths,
            )
            truncation = " (truncated to the configured limit)" if source_snapshot.truncated else ""
            notices.append(
                f"Reviewed a bounded source snapshot: {len(source_snapshot.files)} file(s), "
                f"{source_snapshot.chars} characters{truncation}."
            )
        except HTTPException as exc:
            notices.append(f"Source snapshot was unavailable: {exc.detail}")
    elif non_git_directory:
        notices.append("Source snapshot was disabled; only local tool and test evidence was used.")

    source_context = source_snapshot.content if source_snapshot is not None else None
    if diff_context is not None or source_context is not None or static_issues or test_result is not None:
        try:
            response = await reviewer.review_repository(
                language,
                diff_context,
                source_context,
                static_issues,
                test_result,
            )
            response.generated_by = "deepseek"
            response.llm_status = "succeeded"
        except HTTPException as exc:
            if exc.status_code != 502:
                raise
            response = ReviewResponse(
                summary=_local_evidence_summary(static_issues, test_result),
                issues=static_issues,
                notices=[
                    "DeepSeek 综合分析暂时失败；已保留本地静态分析和自动化测试结果。"
                ],
                generated_by="deepseek",
                llm_status="failed",
            )
    else:
        response = ReviewResponse(summary="No review evidence was collected.", issues=[])

    response.notices.extend(notices)
    response.code_context = _review_code_context(diff_context, source_snapshot, static_issues, test_result)
    if diff_context is not None:
        response.context_chars = len(diff_context)
    elif source_snapshot is not None:
        response.context_files = len(source_snapshot.files)
        response.context_chars = source_snapshot.chars
    if test_result is not None:
        response.test_result = test_result
    return response


def _review_code_context(
    diff_context: str | None,
    source_snapshot: RepositorySourceContext | None,
    static_issues: list[ReviewIssue],
    test_result: TestRunResponse | None,
) -> CodeContext:
    if diff_context is not None:
        return "git_diff"
    if source_snapshot is not None:
        return "source_snapshot"
    if test_result is not None and not static_issues:
        return "test_logs_only"
    if test_result is not None or static_issues:
        return "local_evidence_only"
    return "none"


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


def _diff_unavailable_notice(exc: HTTPException, base_branch: str) -> str:
    if _is_no_diff_error(exc):
        return f"No diff found for {base_branch}...HEAD; skipped Git diff review."
    return "Path is not a Git repository; skipped Git diff review."


def _local_evidence_summary(
    static_issues: list[ReviewIssue], test_result: TestRunResponse | None
) -> str:
    if test_result and test_result.test_status == "failed":
        collected = test_result.collected_cases or 0
        failed = test_result.failed_cases or 0
        return (
            f"本地检查已完成：pytest 收集 {collected} 个用例，其中 {failed} 个失败；"
            "DeepSeek 综合分析暂不可用。"
        )
    if test_result and test_result.test_status == "passed":
        passed = test_result.passed_cases or 0
        return (
            f"本地检查已完成：pytest 的 {passed} 个用例通过；"
            f"静态分析发现 {len(static_issues)} 个问题；DeepSeek 综合分析暂不可用。"
        )
    return (
        f"本地检查已完成：静态分析发现 {len(static_issues)} 个问题；"
        "DeepSeek 综合分析暂不可用。"
    )
