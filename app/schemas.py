from typing import Literal

from pydantic import BaseModel, Field, model_validator


Severity = Literal["low", "medium", "high"]


class ReviewIssue(BaseModel):
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
    repository_path: str | None = Field(default=None, description="本地 Git 仓库或代码目录路径")

    @model_validator(mode="after")
    def require_code_or_repository(self) -> "ReviewRequest":
        if not self.code and not self.repository_path:
            raise ValueError("code 或 repository_path 至少提供一个")
        return self
