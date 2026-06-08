from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from src.app.services.groq_service import transcribe_audio_bytes
from src.app.utils.language import normalize_transcription_language

router = APIRouter(tags=["transcribe"])


@router.get("/")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/transcribe")
async def transcribe_and_run_flow(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
) -> dict[str, str]:
    raw_content = await file.read()
    if not raw_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No audio file content received.",
        )

    normalized_language = normalize_transcription_language(language)
    transcription = await run_in_threadpool(
        transcribe_audio_bytes,
        raw_content,
        file.filename or "command.webm",
        file.content_type,
        normalized_language,
    )
    return {"transcription": transcription}
