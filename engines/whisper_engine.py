"""
Whisper Engine — Local AI subtitle extraction using OpenAI Whisper.
Model: 'small' (244MB, good for Asian languages).
Auto-downloads model on first use.
"""
import os
import asyncio
import logging
from typing import Optional

logger = logging.getLogger("SubtitleExtractor.Whisper")

_whisper_model = None
MODEL_SIZE = "small"


def _get_model(model_size: str = MODEL_SIZE):
    """Load Whisper model (singleton, auto-download)."""
    global _whisper_model
    if _whisper_model is None or _whisper_model._model_size != model_size:
        import whisper
        logger.info(f"Loading Whisper model '{model_size}'... (first time may download ~244MB)")
        _whisper_model = whisper.load_model(model_size)
        _whisper_model._model_size = model_size
        logger.info(f"Whisper model '{model_size}' loaded successfully.")
    return _whisper_model


def _transcribe_sync(file_path: str, language: Optional[str] = None, model_size: str = MODEL_SIZE) -> dict:
    """Synchronous transcription (runs in executor)."""
    import shutil
    import tempfile
    import uuid

    model = _get_model(model_size)

    # ── Handle non-ASCII filenames by copying to a safe temp path ──
    safe_path = file_path
    temp_dir = None
    try:
        basename = os.path.basename(file_path)
        # Check if filename has non-ASCII characters
        try:
            basename.encode('ascii')
        except UnicodeEncodeError:
            temp_dir = tempfile.mkdtemp(prefix="whisper_")
            ext = os.path.splitext(basename)[1] or ".mp4"
            safe_name = f"{uuid.uuid4().hex[:12]}{ext}"
            safe_path = os.path.join(temp_dir, safe_name)
            shutil.copy2(file_path, safe_path)
            logger.info(f"Copied to safe path: {safe_path}")
    except Exception as e:
        logger.warning(f"Safe path copy failed, using original: {e}")
        safe_path = file_path

    options = {
        "verbose": False,
        "word_timestamps": False,
    }
    if language:
        options["language"] = language

    try:
        result = model.transcribe(safe_path, **options)
    finally:
        # Clean up temp copy
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass

    subtitles = []
    for seg in result.get("segments", []):
        subtitles.append({
            "start": round(seg["start"], 3),
            "end": round(seg["end"], 3),
            "text": seg["text"].strip(),
        })

    detected_lang = result.get("language", language or "unknown")

    # Calculate duration from last segment
    duration = subtitles[-1]["end"] if subtitles else 0

    return {
        "status": "success",
        "subtitles": subtitles,
        "language": detected_lang,
        "duration": duration,
        "engine": "whisper",
        "model": model_size,
        "count": len(subtitles),
    }


async def extract_whisper(
    file_path: str,
    language: Optional[str] = None,
    model_size: str = MODEL_SIZE,
) -> dict:
    """
    Extract subtitles using local Whisper model.

    Args:
        file_path: Path to video/audio file
        language: ISO language code (e.g., 'vi', 'en', 'zh'). None = auto-detect.
        model_size: Whisper model size ('tiny', 'base', 'small', 'medium', 'large')

    Returns:
        {"status": "success", "subtitles": [...], "language": str, "duration": float}
    """
    if not os.path.isfile(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _transcribe_sync, file_path, language, model_size)
        return result
    except ImportError:
        return {
            "status": "error",
            "message": "Whisper chưa được cài. Chạy: pip install openai-whisper",
        }
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Whisper error: {error_msg}")
        # Detect no-audio-stream errors
        if "does not contain any stream" in error_msg or "no audio" in error_msg.lower():
            return {"status": "error", "message": "Video không có audio stream. Không thể tách phụ đề từ video im lặng."}
        return {"status": "error", "message": error_msg}

