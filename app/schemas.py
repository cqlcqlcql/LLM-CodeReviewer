from typing import Literal

from pydantic import BaseModel, Field, model_validator


Severity = Literal["low", "medium", "high"]
TestStatus = Literal["passed", "failed", "no_tests", "timeout", "unsupported", "error"]
LlmStatus = Literal["succeeded", "failed", "skipped"]
CodeContext = Literal[
    "git_diff",
    "source_snapshot",
    "test_logs_only",
    "local_evidence_only",
    "none",
]


class ReviewIssue(BaseModel):
    file_path: str | None = None
    source: str = Field(default="LLM", description="Deprecated display alias derived from evidence_sources")
    evidence_sources: list[str] = Field(
        default_factory=list,
        description="Evidence that supports the issue, such as pytest, ruff, or LLM",
    )
    severity: Severity
    category: str
    line: int | None = Field(default=None, ge=1)
    message: str
    suggestion: str

    @model_validator(mode="after")
    def normalize_evidence_sources(self) -> "ReviewIssue":
        sources = self.evidence_sources or self.source.split("+")
        normalized: list[str] = []
        for source in sources:
            value = source.strip()
            if value and value not in normalized:
                normalized.append(value)
        self.evidence_sources = normalized or ["LLM"]
        self.source = " + ".join(self.evidence_sources)
        return self


class ReviewResponse(BaseModel):
    summary: str
    issues: list[ReviewIssue]
    notices: list[str] = Field(default_factory=list, description="Non-issue status messages for the review run")
    test_result: "TestRunResponse | None" = None
    generated_by: str | None = Field(default=None, description="Provider that generated the synthesized review")
    llm_status: LlmStatus = "skipped"
    code_context: CodeContext = "none"
    context_files: int = Field(default=0, ge=0)
    context_chars: int = Field(default=0, ge=0)


class ReviewRequest(BaseModel):
    language: str = Field(default="python", examples=["python"])
    repository_path: str = Field(description="Local Git or non-Git project path")
    base_branch: str = Field(default="main", description="Git base branch used for repository diff reviews")
    run_tests: bool = Field(default=False, description="Run project tests for repository reviews")
    run_static_analysis: bool = Field(default=True, description="Run local static analysis tools for repository reviews")
    include_source_context: bool = Field(
        default=True,
        description="Send a bounded source snapshot when the directory is not a Git repository",
    )

class TestRunRequest(BaseModel):
    repository_path: str = Field(description="Local project path")
    language: str | None = Field(default=None, description="Optional language hint")
    timeout_seconds: int = Field(default=60, ge=1, le=300)


class TestRunResponse(BaseModel):
    test_status: TestStatus
    command: str | None
    collected_cases: int | None = None
    passed_cases: int | None = None
    failed_cases: int | None = None
    skipped_cases: int | None = None
    error_cases: int | None = None
    log_excerpt: str
    llm_explanation: str | None = None


class DiffRequest(BaseModel):
    repository_path: str = Field(description="Local Git repository path")
    base_branch: str = Field(default="main", description="Git base branch used for repository diff reviews")


class ChangedFileSummary(BaseModel):
    path: str
    additions: int
    deletions: int
    hunks: int


class DiffResponse(BaseModel):
    base_branch: str
    diff: str
    files: list[ChangedFileSummary]
