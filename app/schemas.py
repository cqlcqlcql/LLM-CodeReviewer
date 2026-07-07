from typing import Literal

from pydantic import BaseModel, Field, model_validator


Severity = Literal["low", "medium", "high"]
TestStatus = Literal["passed", "failed", "timeout", "unsupported", "error"]


class ReviewIssue(BaseModel):
    file_path: str | None = None
    source: str = Field(default="LLM", description="Tool or reviewer that reported the issue")
    severity: Severity
    category: str
    line: int | None = Field(default=None, ge=1)
    message: str
    suggestion: str


class ReviewResponse(BaseModel):
    summary: str
    issues: list[ReviewIssue]
    notices: list[str] = Field(default_factory=list, description="Non-issue status messages for the review run")
    test_result: "TestRunResponse | None" = None


class ReviewRequest(BaseModel):
    language: str = Field(default="python", examples=["python"])
    code: str | None = Field(default=None, examples=["def add(a,b): return a-b"])
    repository_path: str | None = Field(default=None, description="Local Git repository path")
    base_branch: str = Field(default="main", description="Git base branch used for repository diff reviews")
    run_tests: bool = Field(default=False, description="Run project tests for repository reviews")
    run_static_analysis: bool = Field(default=True, description="Run local static analysis tools for repository reviews")

    @model_validator(mode="after")
    def require_code_or_repository(self) -> "ReviewRequest":
        if not self.code and not self.repository_path:
            raise ValueError("code or repository_path is required")
        return self


class TestRunRequest(BaseModel):
    repository_path: str = Field(description="Local project path")
    language: str | None = Field(default=None, description="Optional language hint")
    timeout_seconds: int = Field(default=60, ge=1, le=300)


class TestRunResponse(BaseModel):
    test_status: TestStatus
    command: str | None
    failed_cases: int | None = None
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
