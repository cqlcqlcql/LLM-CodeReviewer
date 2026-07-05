from typing import Literal

from pydantic import BaseModel, Field, model_validator


Severity = Literal["low", "medium", "high"]


class ReviewIssue(BaseModel):
    file_path: str | None = None
    severity: Severity
    category: str
    line: int | None = Field(default=None, ge=1)
    message: str
    suggestion: str


class ReviewResponse(BaseModel):
    summary: str
    issues: list[ReviewIssue]


class ReviewRequest(BaseModel):
    language: str = Field(default="python", examples=["python"])
    code: str | None = Field(default=None, examples=["def add(a,b): return a-b"])
    repository_path: str | None = Field(default=None, description="Local Git repository path")

    @model_validator(mode="after")
    def require_code_or_repository(self) -> "ReviewRequest":
        if not self.code and not self.repository_path:
            raise ValueError("code or repository_path is required")
        return self
