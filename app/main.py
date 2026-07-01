from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.schemas import ReviewRequest, ReviewResponse
from app.services.code_loader import trim_code
from app.services.diff_loader import load_repository_diff
from app.services.llm import build_reviewer
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
        diff_context = load_repository_diff(payload.repository_path, settings.max_code_chars)
        return await reviewer.review_diff(payload.language, diff_context)

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
