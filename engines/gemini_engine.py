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


def _tool(name: str) -> str:
    """Absolute path to ffmpeg/ffprobe, or the bare name as a last resort.

    tubecli is asked FIRST, and shutil.which is only the fallback — the reverse
    of what this used to do.

    which() answers "is there a file with this name on PATH", not "does it
    start". A conda install ships Library/bin/ffprobe.exe that dies at load with
    exit 3221225785 (0xC0000139 STATUS_ENTRYPOINT_NOT_FOUND) because a sibling
    DLL does not export a symbol it links against, and the server runs on that
    same conda Python — so which() returned the broken copy, this function
    accepted it, and the tubecli lookup below never ran even though a working
    ffprobe sat further down the very same PATH. The user got
    "non-zero exit status 3221225785" and was told to install FFmpeg, which was
    already installed.

    tubecli's resolver probes each candidate with -version and skips the ones
    that do not start, so it is the one that should win.
    """
    try:
        from tubecli.extensions.video_studio.ffmpeg_utils import find_ffmpeg, find_ffprobe

        found = find_ffmpeg() if name == "ffmpeg" else find_ffprobe()
        if found:
            return found
    except Exception:
        pass

    # tubecli unavailable (extension used standalone) or it found nothing.
    import shutil

    found = shutil.which(name)
    if found:
        return found
    return name


def _split_audio_ffmpeg(file_path: str, chunk_duration: int = CHUNK_DURATION) -> List[Dict]:
    """Split audio/video into chunks using FFmpeg."""
    # Get total duration
    probe_cmd = [
        _tool("ffprobe"), "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", file_path
    ]
    # A timeout, and stderr is kept. This was the one subprocess call in the
    # extension with no timeout at all, so a stalled probe hung the request
    # forever — and it threw stderr away, which is the only place ffprobe says
    # WHY it failed.
    try:
        probe = subprocess.run(probe_cmd, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffprobe did not respond within 30s while reading '{os.path.basename(file_path)}'. "
            f"The file may be on a disconnected drive."
        )
    except OSError as e:
        raise RuntimeError(f"Could not run ffprobe ({probe_cmd[0]}): {e}")

    raw = (probe.stdout or b"").decode("utf-8", "replace").strip()
    if probe.returncode != 0 or not raw:
        # "Could not probe" is NOT "the media is 0 seconds long". Conflating
        # them used to return a single pseudo-chunk whose path was the user's
        # ORIGINAL file — which the caller then uploaded whole to Gemini and,
        # worse, deleted in its temp-chunk cleanup.
        #
        # Do not tell the user to install FFmpeg. The usual cause is that a
        # copy IS installed and does not start: a conda build exits
        # 3221225785 (0xC0000139 STATUS_ENTRYPOINT_NOT_FOUND) because a sibling
        # DLL is incompatible. Name the binary that failed so the message
        # points at the real thing.
        tail = (probe.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise RuntimeError(
            f"Could not read the media duration.\n"
            f"ffprobe: {probe_cmd[0]}\n"
            f"exit {probe.returncode}"
            + (f"\n{tail}" if tail else
               " with no output — this usually means the binary is present but cannot start.")
        )
    try:
        duration = float(raw)
    except ValueError:
        raise RuntimeError(f"ffprobe returned an unreadable duration: {raw[:80]!r}")

    if duration <= 0:
        raise RuntimeError(f"The media reports a duration of {duration}s — nothing to transcribe.")

    chunks = []
    tmp_dir = tempfile.mkdtemp(prefix="subtitle_chunks_")
    num_chunks = max(1, int(duration / chunk_duration) + (1 if duration % chunk_duration > 0 else 0))

    for i in range(num_chunks):
        start = i * chunk_duration
        dur = min(chunk_duration, duration - start)
        chunk_path = os.path.join(tmp_dir, f"chunk_{i:03d}.mp3")

        # Extract audio chunk as MP3 (smaller for upload)
        cmd = [
            _tool("ffmpeg"), "-y", "-ss", str(start), "-t", str(dur),
            "-i", file_path, "-vn", "-acodec", "libmp3lame", "-q:a", "4",
            chunk_path
        ]
        # The return code is checked. It used to be ignored, and the only gate
        # was "did a file appear" — so a broken ffmpeg (a conda build that dies
        # at load with 0xC0000139 raises no exception and writes nothing) made
        # every chunk vanish, chunks stayed empty, and the caller reported
        # success with zero subtitles. The user saw "done, 0 lines" for a video
        # full of speech.
        #
        # The timeout scales with the segment: 60s was hard-coded for a 175s
        # chunk, so a slow disk silently deleted three minutes of transcript.
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=max(120, int(dur) * 2),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"ffmpeg timed out extracting audio at {int(start)}s. "
                f"The file may be on a slow or disconnected drive."
            )
        except OSError as e:
            raise RuntimeError(f"Could not run ffmpeg ({cmd[0]}): {e}")

        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(
                f"ffmpeg failed with exit {proc.returncode} while extracting audio "
                f"at {int(start)}s.\nffmpeg: {cmd[0]}\n{tail}"
            )
        if not os.path.exists(chunk_path):
            raise RuntimeError(
                f"ffmpeg reported success but wrote no audio for the segment at "
                f"{int(start)}s ({cmd[0]})."
            )

        chunks.append({
            "index": i,
            "start": start,
            "duration": dur,
            "path": chunk_path,
        })

    # A short split is a hole in the transcript, not a smaller job. Refuse it
    # rather than transcribing whatever survived and calling that the result.
    if not chunks:
        raise RuntimeError("No audio could be extracted from this file.")
    if len(chunks) != num_chunks:
        raise RuntimeError(
            f"Only {len(chunks)} of {num_chunks} audio segments could be extracted; "
            f"the transcript would be missing parts of the video."
        )
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
        # No candidate at all is a refusal or a safety block, not silence in
        # the audio. Returning an empty chunk here dropped 175 seconds while
        # the run still counted it completed.
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise ChunkParseError(
            f"Gemini returned no answer for the segment at {int(chunk_start)}s"
            + (f" (blocked: {block})" if block else ".")
        )

    text = "".join(p.get("text", "") for p in candidates[0].get("content", {}).get("parts", []))

    # Parse JSON from response.
    #
    # An unparseable answer is an ERROR, not an empty chunk. Returning [] here
    # made a truncated reply — which maxOutputTokens=8192 makes reachable on
    # dense speech — silently delete a whole 175-second stretch while the run
    # still counted the chunk as completed and showed a clean 100%.
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if not json_match:
        reason = (candidates[0].get("finishReason") or "").upper()
        if reason == "MAX_TOKENS":
            raise ChunkParseError(
                f"The model hit its output limit on the segment at "
                f"{int(chunk_start)}s, so its answer was cut off mid-list."
            )
        raise ChunkParseError(
            f"The model returned no subtitle list for the segment at "
            f"{int(chunk_start)}s"
            + (f" (finishReason={reason})" if reason else "")
            + f". First 120 chars: {text.strip()[:120]!r}"
        )

    try:
        raw_subs = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        raise ChunkParseError(
            f"The model's subtitle list for the segment at {int(chunk_start)}s "
            f"was not valid JSON: {e}"
        )
    if not isinstance(raw_subs, list):
        raise ChunkParseError(
            f"Expected a list of subtitles for the segment at {int(chunk_start)}s, "
            f"got {type(raw_subs).__name__}."
        )

    # Adjust timestamps relative to full video.
    #
    # Resolve everything in CHUNK-RELATIVE time first, then rebase once. The
    # old code rebased `start`, then computed the missing `end` as
    # `chunk_start + (start + 0.5)` — where `start` was already absolute — so
    # chunk_start was added twice. On chunk 5 (chunk_start 875s) a cue at 3s
    # with no end came out 878 -> 1753.5: a single 875-second subtitle, which
    # also became the reported video duration downstream.
    try:
        _dur = float(chunk.get("duration") or 0)
    except (TypeError, ValueError):
        _dur = 0.0
    # None means "no known end", so clamping is skipped rather than collapsing
    # every cue onto chunk_start.
    chunk_end = (chunk_start + _dur) if _dur > 0 else None
    subtitles = []
    for s in raw_subs:
        try:
            rel_start = float(s.get("start", 0))
            rel_end = float(s.get("end", rel_start + 0.5))
        except (TypeError, ValueError):
            continue
        if rel_end < rel_start:
            rel_start, rel_end = rel_end, rel_start
        start = chunk_start + rel_start
        end = chunk_start + rel_end
        # A hallucinated timestamp must not reach into the next chunk's region,
        # where it would interleave with real cues and break ordering.
        if chunk_end:
            start = min(max(start, chunk_start), chunk_end)
            end = min(max(end, start), chunk_end)
        text_val = str(s.get("text", "")).strip()
        if text_val:
            subtitles.append({"start": round(start, 3), "end": round(end, 3), "text": text_val})

    return {"subtitles": subtitles, "chunk_index": chunk["index"]}


class QuotaExceededError(Exception):
    pass


class ChunkParseError(Exception):
    """The model answered, but its subtitle list could not be read.

    Its own type, so the retry path can match on it instead of sniffing for
    the substring "JSON" in a message — json.JSONDecodeError formats as
    'Expecting property name enclosed in double quotes: line 1 column 3', which
    contains no "JSON" at all, so that branch could never fire and every
    malformed answer silently dropped its chunk.
    """


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

    # Split into chunks.
    #
    # Off the event loop. _split_audio_ffmpeg is fully blocking — one
    # subprocess.run per chunk, 21 of them for an hour of video — and this
    # coroutine is dispatched as a FastAPI background task, so running it
    # inline froze the dashboard, the chat, every other extension route and
    # the status polling for the whole split. whisper_engine.py already does
    # this correctly.
    logger.info(f"Splitting audio from {file_path}...")
    chunks = await asyncio.to_thread(_split_audio_ffmpeg, file_path)
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
            last_error = None
            for attempt in range(max_retries):
                key_obj = get_alive_key()
                if not key_obj:
                    # Every key is out of quota. That is a failed job, not an
                    # empty one — returning [] here produced a "success" with a
                    # silently missing stretch of transcript.
                    raise RuntimeError(
                        "Every Gemini key is out of quota, so the segment at "
                        f"{int(chunk['start'])}s could not be transcribed."
                    )

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
                except ChunkParseError as e:
                    # Match the TYPE. The old test was `"JSON" in str(e)`, and
                    # json.JSONDecodeError renders as "Expecting property name
                    # enclosed in double quotes: line 1 column 3" — no "JSON"
                    # anywhere — so this branch never fired and a malformed
                    # answer dropped its chunk instead of being retried.
                    last_error = e
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1)
                        continue
                    raise
                except Exception as e:
                    if "503" in str(e):
                        last_error = e
                        await asyncio.sleep(1)
                        continue
                    logger.error(f"Chunk {chunk['index']} error: {e}")
                    raise
            # Retries exhausted. Surface the last cause instead of an empty list.
            raise RuntimeError(
                f"Gave up on the segment at {int(chunk['start'])}s after "
                f"{max_retries} attempts"
                + (f": {last_error}" if last_error else ".")
            )

    tasks = [process_with_rotation(chunk) for chunk in chunks]
    # Not return_exceptions=True: a chunk that fails is a hole in the
    # transcript, and the job must fail loudly rather than hand back a shorter
    # result that looks complete.
    results = await asyncio.gather(*tasks)

    for subs in results:
        all_subtitles.extend(subs)

    # Sort by start time
    all_subtitles.sort(key=lambda s: s["start"])

    # Cleanup temp chunks. Only ever delete files we created under the system
    # temp directory — a chunk path that points at the caller's own media must
    # never be removed here.
    tmp_root = os.path.realpath(tempfile.gettempdir())
    for chunk in chunks:
        path = chunk.get("path") or ""
        try:
            if os.path.realpath(path).startswith(tmp_root + os.sep):
                os.remove(path)
            else:
                logger.warning(f"refusing to delete a non-temp file: {path}")
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
