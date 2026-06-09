"""
Gemini Engine — Cloud AI subtitle extraction using Google Gemini.
Uses KeyManager for API keys with auto-rotation on quota errors.
Processes long videos by chunking audio and running concurrent requests.
"""
import os
import re
import json
import asyncio
import logging
import subprocess
import tempfile
from typing import Optional, List, Dict

logger = logging.getLogger("SubtitleExtractor.Gemini")

CHUNK_DURATION = 175  # seconds per chunk
MAX_CONCURRENCY = 3
GEMINI_MODEL = "gemini-2.5-flash"


def _get_api_keys() -> List[str]:
    """Get available Gemini API keys from KeyManager."""
    try:
        from tubecli.extensions.cloud_api.extension import key_manager
        keys_data = key_manager._keys.get("gemini", {})
        active_keys = []
        for label, entry in keys_data.items():
            if isinstance(entry, dict) and entry.get("active") and entry.get("key"):
                active_keys.append(entry["key"])
        # Fallback: env var
        if not active_keys:
            env_key = os.environ.get("GEMINI_API_KEY")
            if env_key:
                active_keys.append(env_key)
        return active_keys
    except Exception as e:
        logger.warning(f"KeyManager import failed: {e}")
        env_key = os.environ.get("GEMINI_API_KEY")
        return [env_key] if env_key else []


def _split_audio_ffmpeg(file_path: str, chunk_duration: int = CHUNK_DURATION) -> List[Dict]:
    """Split audio/video into chunks using FFmpeg."""
    # Get total duration
    probe_cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    try:
        duration = float(subprocess.check_output(probe_cmd, stderr=subprocess.DEVNULL).decode().strip())
    except Exception:
        duration = 0

    if duration <= 0:
        return [{"index": 0, "start": 0, "duration": 0, "path": file_path}]

    chunks = []
    tmp_dir = tempfile.mkdtemp(prefix="subtitle_chunks_")
    num_chunks = max(1, int(duration / chunk_duration) + (1 if duration % chunk_duration > 0 else 0))

    for i in range(num_chunks):
        start = i * chunk_duration
        dur = min(chunk_duration, duration - start)
        chunk_path = os.path.join(tmp_dir, f"chunk_{i:03d}.mp3")

        # Extract audio chunk as MP3 (smaller for upload)
        cmd = [
            "ffmpeg", "-y", "-ss", str(start), "-t", str(dur),
            "-i", file_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4",
            chunk_path
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except Exception as e:
            logger.warning(f"Chunk {i} extraction failed: {e}")
            continue

        if os.path.exists(chunk_path):
            chunks.append({
                "index": i,
                "start": start,
                "duration": dur,
                "path": chunk_path,
            })

    return chunks


async def _process_chunk(chunk: Dict, api_key: str, language: Optional[str], translate_to: Optional[str]) -> Dict:
    """Process a single audio chunk with Gemini API."""
    import base64
    import httpx

    chunk_path = chunk["path"]
    chunk_start = chunk["start"]

    # Read audio file
    with open(chunk_path, "rb") as f:
        audio_bytes = f.read()
    audio_b64 = base64.b64encode(audio_bytes).decode()

    # Build prompt
    prompt = "You are an expert subtitle creator. Analyze the attached audio segment.\n\n"
    prompt += "TASK:\n1. Transcribe all speech accurately.\n"
    if translate_to:
        prompt += f"2. Translate every sentence into {translate_to}. The 'text' field MUST be the translation.\n"
    prompt += """
RULES:
- Ignore background music/noise.
- One sentence per subtitle entry.
- Use floating-point seconds for timestamps (relative to this segment start = 0).

OUTPUT FORMAT (JSON Array only, no other text):
[
  {"start": 0.0, "end": 2.5, "text": "Subtitle text here"},
  {"start": 2.5, "end": 5.1, "text": "Next line"}
]
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "audio/mp3", "data": audio_b64}}
            ]
        }],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload)

    if resp.status_code == 429:
        raise QuotaExceededError(f"Key exhausted (429)")
    if resp.status_code != 200:
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return {"subtitles": [], "chunk_index": chunk["index"]}

    text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))

    # Parse JSON from response
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if not json_match:
        return {"subtitles": [], "chunk_index": chunk["index"]}

    raw_subs = json.loads(json_match.group())

    # Adjust timestamps relative to full video
    subtitles = []
    for s in raw_subs:
        start = chunk_start + float(s.get("start", 0))
        end = chunk_start + float(s.get("end", start + 0.5))
        text_val = str(s.get("text", "")).strip()
        if text_val:
            subtitles.append({"start": round(start, 3), "end": round(end, 3), "text": text_val})

    return {"subtitles": subtitles, "chunk_index": chunk["index"]}


class QuotaExceededError(Exception):
    pass


async def extract_gemini(
    file_path: str,
    language: Optional[str] = None,
    translate_to: Optional[str] = None,
    model: str = GEMINI_MODEL,
    progress_callback=None,
) -> dict:
    """
    Extract subtitles using Gemini AI with chunk processing and key rotation.

    Args:
        file_path: Path to video/audio file
        language: Source language hint
        translate_to: Target translation language (None = transcribe only)
        model: Gemini model name
        progress_callback: async fn(completed, total) for progress updates
    """
    global GEMINI_MODEL
    GEMINI_MODEL = model

    api_keys = _get_api_keys()
    if not api_keys:
        return {"status": "error", "message": "Chưa có Gemini API key. Thêm qua: tubecli cloud add gemini <key>"}

    if not os.path.isfile(file_path):
        return {"status": "error", "message": f"File not found: {file_path}"}

    # Split into chunks
    logger.info(f"Splitting audio from {file_path}...")
    chunks = _split_audio_ffmpeg(file_path)
    total = len(chunks)
    logger.info(f"Created {total} chunks")

    # Key pool with rotation
    key_pool = [{"key": k, "alive": True} for k in api_keys]
    key_index = 0

    def get_alive_key():
        nonlocal key_index
        for _ in range(len(key_pool)):
            k = key_pool[key_index % len(key_pool)]
            key_index += 1
            if k["alive"]:
                return k
        return None

    # Process chunks concurrently
    all_subtitles = []
    completed = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def process_with_rotation(chunk):
        nonlocal completed
        async with semaphore:
            max_retries = len(key_pool) + 2
            for attempt in range(max_retries):
                key_obj = get_alive_key()
                if not key_obj:
                    logger.error("All Gemini keys exhausted!")
                    return []

                try:
                    result = await _process_chunk(chunk, key_obj["key"], language, translate_to)
                    completed += 1
                    if progress_callback:
                        await progress_callback(completed, total)
                    return result.get("subtitles", [])
                except QuotaExceededError:
                    key_obj["alive"] = False
                    logger.warning(f"Key ...{key_obj['key'][-4:]} quota exceeded, rotating...")
                    # Report to KeyManager
                    try:
                        from tubecli.extensions.cloud_api.extension import key_manager
                        key_manager.report_key_error("gemini", key_obj["key"], "Subtitle extraction: Quota exceeded")
                    except Exception:
                        pass
                    continue
                except Exception as e:
                    if "503" in str(e) or "JSON" in str(e):
                        await asyncio.sleep(1)
                        continue
                    logger.error(f"Chunk {chunk['index']} error: {e}")
                    return []
            return []

    tasks = [process_with_rotation(chunk) for chunk in chunks]
    results = await asyncio.gather(*tasks)

    for subs in results:
        all_subtitles.extend(subs)

    # Sort by start time
    all_subtitles.sort(key=lambda s: s["start"])

    # Cleanup temp chunks
    for chunk in chunks:
        try:
            os.remove(chunk["path"])
        except Exception:
            pass

    # Get duration
    duration = all_subtitles[-1]["end"] if all_subtitles else 0

    return {
        "status": "success",
        "subtitles": all_subtitles,
        "language": language or "auto",
        "duration": duration,
        "engine": "gemini",
        "model": model,
        "count": len(all_subtitles),
        "chunks_processed": completed,
        "chunks_total": total,
    }
