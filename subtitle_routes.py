"""
Subtitle Extractor API Routes — FastAPI router for all subtitle operations.
Self-contained: serves both HTML page and API endpoints.
"""
import os
import json
import asyncio
import logging
import shutil
import time
from typing import Optional, List
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

logger = logging.getLogger("SubtitleExtractor.API")

# ── Extension directory (auto-detected) ──
_EXT_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_EXT_DIR, "static")


def _data_root() -> str:
    """This extension's own data directory, created on demand."""
    try:
        from tubecli.config import ext_data_path

        root = str(ext_data_path("subtitle_extractor"))
    except Exception:
        from tubecli.config import DATA_DIR

        root = os.path.join(str(DATA_DIR), "subtitle_extractor")
    os.makedirs(root, exist_ok=True)
    return root


def _exports_dir() -> str:
    d = os.path.join(_data_root(), "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _contain(path: str, root: str, label: str) -> str:
    """Resolve a caller-supplied path and refuse anything outside `root`.

    realpath on both sides, so "..", a symlink and a Windows junction are all
    covered — checking the raw string would let "%s/../../..." through, and
    on this install data/ is full of junctions pointing elsewhere.
    """
    resolved_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(resolved_root, path))
    if target != resolved_root and not target.startswith(resolved_root + os.sep):
        raise HTTPException(
            403,
            f"{label} must stay inside {resolved_root} (got: {path})",
        )
    os.makedirs(os.path.dirname(target) or resolved_root, exist_ok=True)
    return target


_ASS_COLOURS = {
    "white": "&H00FFFFFF", "black": "&H00000000", "yellow": "&H0000FFFF",
    "red": "&H000000FF", "green": "&H0000FF00", "blue": "&H00FF0000",
    "cyan": "&H00FFFF00", "magenta": "&H00FF00FF", "orange": "&H000080FF",
}


def _ass_colour(value: str) -> str:
    """Map a colour name or #RRGGBB to the ASS &HAABBGGRR form.

    BurnRequest.font_color existed and was never read — the style string
    hardcoded white — so the API accepted a colour and quietly ignored it.
    Note the byte order is REVERSED from HTML: ASS is BGR, so #FF0000 (red)
    becomes &H000000FF.
    """
    v = (value or "").strip().lower()
    if v in _ASS_COLOURS:
        return _ASS_COLOURS[v]
    if v.startswith("#") and len(v) == 7:
        try:
            r, g, b = v[1:3], v[3:5], v[5:7]
            int(r + g + b, 16)
            return f"&H00{b}{g}{r}".upper().replace("&HOO", "&H00")
        except ValueError:
            pass
    return _ASS_COLOURS["white"]


def _ffmpeg_exe() -> str:
    """Path to an ffmpeg that actually starts, or the bare name as a fallback.

    Passing a bare "ffmpeg" lets the OS pick the first file with that name and
    it does not care whether the thing runs. On a conda-based install that
    first hit is Library/bin/ffmpeg.exe, which dies at load with 0xC0000139
    STATUS_ENTRYPOINT_NOT_FOUND — and a launcher-started server often has a
    PATH so thin the bare name finds nothing at all.

    tubecli's resolver probes each candidate with -version and skips the ones
    that do not start, so ask it first. Deliberately not imported from
    engines/gemini_engine.py: this file loads its engines through
    importlib.util.spec_from_file_location, so "engines.gemini_engine" is not
    an importable module name here and the import would raise inside the burn
    task's except block as a confusing unrelated error.
    """
    try:
        from tubecli.extensions.video_studio.ffmpeg_utils import find_ffmpeg

        found = find_ffmpeg()
        if found:
            return found
    except Exception:
        pass
    return shutil.which("ffmpeg") or "ffmpeg"

# ── Single Router (serves both pages and API) ──
router = APIRouter(tags=["Subtitle Extractor"])


# ═══════════════════════════════════════════════════
# ── Page Routes (no prefix) ──
# ═══════════════════════════════════════════════════

@router.get("/subtitle-extractor", response_class=HTMLResponse)
async def subtitle_extractor_page():
    """Serve the Subtitle Extractor HTML page."""
    html_path = os.path.join(_STATIC_DIR, "subtitle.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    raise HTTPException(404, "Subtitle Extractor page not found")


@router.get("/subtitle-extractor-static/{filename:path}")
async def subtitle_extractor_static(filename: str):
    """Serve static files (CSS, JS, i18n) for the Subtitle Extractor UI."""
    filepath = os.path.join(_STATIC_DIR, filename)
    if os.path.exists(filepath) and os.path.isfile(filepath):
        return FileResponse(filepath)
    raise HTTPException(404, f"Static file not found: {filename}")

# Upload ceiling. Nothing limited this before, and the uploads directory
# had grown to 540 MB — including three byte-identical copies of one
# 45 MB mp4, because the page uploads the same File again for Burn.
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# ── In-memory task tracking ──
_tasks = {}


class ExtractRequest(BaseModel):
    file_path: str
    engine: str = "whisper"  # whisper | gemini | youtube
    language: Optional[str] = None
    translate_to: Optional[str] = None
    model: Optional[str] = None


class YouTubeExtractRequest(BaseModel):
    url: str
    languages: Optional[List[str]] = None


class ExportRequest(BaseModel):
    subtitles: List[dict]
    format: str = "srt"  # srt | json | vtt | ass
    output_path: Optional[str] = None


class BurnRequest(BaseModel):
    video_path: str
    srt_path: str
    output_path: Optional[str] = None
    font_size: int = 24
    font_color: str = "white"
    position: str = "bottom"  # bottom | top | center


class TranslateRequest(BaseModel):
    subtitles: List[dict]
    target_language: str
    source_language: Optional[str] = None


# ── Extract Endpoints ──

@router.post("/api/v1/subtitle/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file to the subtitle extractor workspace"""
    import uuid

    # The canonical location. data/subtitle_extractor is a junction onto this
    # same directory, so no existing upload moves — but naming the real place
    # is what lets the compatibility junction be retired.
    upload_dir = os.path.join(_data_root(), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    # Only the extension is taken from the client's filename, and only if it
    # looks like one — the rest is a uuid, so a crafted name cannot steer the
    # write anywhere.
    ext = os.path.splitext(file.filename or "")[1][:10]
    if not ext.isascii() or any(c in ext for c in '\\/:*?"<>|'):
        ext = ""
    file_path = os.path.join(upload_dir, f"{uuid.uuid4().hex}{ext}")

    # Streamed in bounded chunks off the event loop, with a size cap. The old
    # code called shutil.copyfileobj synchronously inside an async handler, so
    # the whole dashboard froze for the length of a several-hundred-MB
    # transfer, and nothing limited how much a caller could write.
    written = 0
    try:
        with open(file_path, "wb") as buffer:
            while True:
                block = await file.read(1 << 20)
                if not block:
                    break
                written += len(block)
                if written > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"File is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
                    )
                await asyncio.to_thread(buffer.write, block)
    except Exception:
        # Never leave a partial upload behind for a later extract to pick up.
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise

    return {"success": True, "path": file_path, "size": written}


@router.post("/api/v1/subtitle/extract")
async def extract_subtitles(body: ExtractRequest, background_tasks: BackgroundTasks):
    """Extract subtitles from a local file using specified engine."""
    if not os.path.isfile(body.file_path):
        raise HTTPException(404, f"File not found: {body.file_path}")

    import uuid
    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = {"status": "processing", "progress": 0, "total": 0, "result": None}

    async def _run():
        try:
            engines_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")
            import importlib.util

            if body.engine == "gemini":
                spec = importlib.util.spec_from_file_location("gem", os.path.join(engines_dir, "gemini_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

                async def progress_cb(completed, total):
                    _tasks[task_id]["progress"] = completed
                    _tasks[task_id]["total"] = total

                result = await mod.extract_gemini(
                    body.file_path,
                    language=body.language,
                    translate_to=body.translate_to,
                    model=body.model or "gemini-2.5-flash",
                    progress_callback=progress_cb,
                )
            elif body.engine == "whisper":
                spec = importlib.util.spec_from_file_location("wh", os.path.join(engines_dir, "whisper_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_whisper(
                    body.file_path,
                    language=body.language,
                    model_size=body.model or "small",
                )
            else:
                result = {"status": "error", "message": f"Unknown engine: {body.engine}. Use 'whisper' or 'gemini'."}

            # ── Post-extraction Translation ──
            if result.get("status") == "success" and body.translate_to and body.engine != "gemini":
                import httpx
                import re
                subs = result.get("subtitles", [])
                target_lang = body.translate_to
                total_subs = len(subs)
                
                _tasks[task_id]["progress"] = 0
                _tasks[task_id]["total"] = total_subs
                
                translated_subs = []
                chunk_size = 25
                for i in range(0, total_subs, chunk_size):
                    chunk = subs[i:i+chunk_size]
                    batch_text = "\n".join(f"{idx+1}. {s.get('text', '')}" for idx, s in enumerate(chunk))
                    prompt = (
                        f"Translate the following numbered lines to {target_lang}. "
                        f"Return ONLY a raw JSON array of translated strings in the exact same order. "
                        f"Do not include markdown formatting, code fences, or any other text.\n\n{batch_text}"
                    )
                    
                    try:
                        async with httpx.AsyncClient(timeout=300) as client:
                            resp = await client.post(
                                "http://127.0.0.1:5295/api/v1/localai/chat/completions",
                                json={"messages": [{"role": "user", "content": prompt}]}
                            )
                        
                        if resp.status_code == 200:
                            data = resp.json()
                            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                            json_match = re.search(r'\[.*\]', content, re.DOTALL)
                            if json_match:
                                translations = json.loads(json_match.group())
                                for j, sub in enumerate(chunk):
                                    new_sub = dict(sub)
                                    if j < len(translations):
                                        new_sub["text"] = str(translations[j])
                                    translated_subs.append(new_sub)
                                
                                _tasks[task_id]["progress"] = min(i + chunk_size, total_subs)
                                logger.info(f"Translation progress: {i + chunk_size}/{total_subs}")
                                continue
                    except Exception as e:
                        logger.error(f"Translation chunk error: {e}")
                    
                    # Fallback: keep original if translation failed
                    translated_subs.extend(chunk)
                    _tasks[task_id]["progress"] = min(i + chunk_size, total_subs)
                
                result["subtitles"] = translated_subs
                result["translated_to"] = target_lang

            _tasks[task_id]["status"] = result.get("status", "error")
            _tasks[task_id]["result"] = result
        except Exception as e:
            logger.error(f"Extraction task error: {e}")
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["result"] = {"status": "error", "message": str(e)}

    background_tasks.add_task(_run)
    return {"success": True, "task_id": task_id, "message": f"Extraction started with {body.engine}"}


@router.post("/api/v1/subtitle/extract/youtube")
async def extract_youtube_subtitles(body: YouTubeExtractRequest):
    """Extract subtitles from a YouTube video (CC)."""
    import importlib.util
    engines_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")
    spec = importlib.util.spec_from_file_location("yt", os.path.join(engines_dir, "youtube_engine.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    result = await mod.extract_youtube(body.url, languages=body.languages)
    if result.get("status") == "error":
        raise HTTPException(400, result.get("message", "YouTube extraction failed"))
    return {"success": True, **result}


@router.get("/api/v1/subtitle/status/{task_id}")
async def get_task_status(task_id: str):
    """Get extraction task status and progress."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"success": True, "task_id": task_id, **task}


# ── Export Endpoints ──

@router.post("/api/v1/subtitle/export")
async def export_subtitles(body: ExportRequest):
    """Export subtitles to SRT, JSON, VTT, or ASS format."""
    subs = body.subtitles
    fmt = body.format.lower()

    if fmt == "srt":
        content = _to_srt(subs)
        ext = ".srt"
    elif fmt == "json":
        content = json.dumps(subs, indent=2, ensure_ascii=False)
        ext = ".json"
    elif fmt == "vtt":
        content = _to_vtt(subs)
        ext = ".vtt"
    elif fmt == "ass":
        content = _to_ass(subs)
        ext = ".ass"
    else:
        raise HTTPException(400, f"Unsupported format: {fmt}")

    out_dir = _exports_dir()
    if body.output_path:
        # Contained, not trusted. This used to take output_path verbatim and
        # write it — and with format="json" the content is whatever the caller
        # posted, so it was a full arbitrary-file-write primitive on an API
        # that has no authentication and no CSRF token anywhere in the router.
        out_path = _contain(body.output_path, out_dir, "output_path")
        if os.path.splitext(out_path)[1].lower() != ext:
            out_path += ext
    else:
        # A unique name per export. Everything used to land on the single
        # shared filename "subtitles<ext>", so two jobs overwrote each other
        # and Burn could pick up a different job's subtitles.
        import uuid

        stamp = time.strftime("%Y%m%d-%H%M%S")
        out_path = os.path.join(out_dir, f"subtitles_{stamp}_{uuid.uuid4().hex[:6]}{ext}")

    # utf-8-sig for SRT only: players that assume cp1252 turn "Xin chào" into
    # "Xin chÃ o" without the BOM, but a WEBVTT file must start with its magic
    # and JSON must not carry one either.
    enc = "utf-8-sig" if ext == ".srt" else "utf-8"
    with open(out_path, "w", encoding=enc, newline="") as f:
        f.write(content)

    return {"success": True, "path": out_path, "format": fmt, "count": len(subs)}


# ── Burn Subtitle into Video ──

@router.post("/api/v1/subtitle/burn")
async def burn_subtitles(body: BurnRequest, background_tasks: BackgroundTasks):
    """Burn SRT subtitles into video using FFmpeg."""
    if not os.path.isfile(body.video_path):
        raise HTTPException(404, f"Video not found: {body.video_path}")
    if not os.path.isfile(body.srt_path):
        raise HTTPException(404, f"SRT not found: {body.srt_path}")

    # Contained the same way /export is: a caller-chosen absolute path
    # here would let any page on the machine overwrite any file the
    # server can write, with an mp4 of its choosing.
    output = _contain(body.output_path, _exports_dir(), "output_path") \
        if body.output_path else ""
    if not output:
        base = os.path.splitext(body.video_path)[0]
        output = f"{base}_subtitled.mp4"

    import uuid
    task_id = uuid.uuid4().hex[:8]
    _tasks[task_id] = {"status": "processing", "result": None}

    async def _burn():
        try:
            # Build subtitle filter
            # force_style uses LEGACY SSA v4 alignment, where the tens digit is
            # the vertical band: 1-3 bottom, 5-7 top, 9-11 middle. "center"
            # used to send 5, which is TOP-LEFT — measured by burning one frame
            # per value: default landed at rows 215-227, Alignment=6 at rows
            # 11-24 cols 132-188 (top-centre, correct), Alignment=5 at rows
            # 11-24 cols 9-65 (top-LEFT, wrong). 10 is middle-centre.
            style = f"FontSize={body.font_size},PrimaryColour={_ass_colour(body.font_color)}"
            if body.position == "top":
                style += ",Alignment=6"
            elif body.position == "center":
                style += ",Alignment=10"

            srt_escaped = body.srt_path.replace("\\", "/").replace(":", "\\:")
            vf = f"subtitles='{srt_escaped}':force_style='{style}'"

            cmd = [
                _ffmpeg_exe(), "-y", "-i", body.video_path,
                "-vf", vf,
                "-c:a", "copy",
                output
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

            if proc.returncode == 0 and os.path.exists(output):
                _tasks[task_id]["status"] = "success"
                _tasks[task_id]["result"] = {
                    "status": "success",
                    "output": output,
                    "size": os.path.getsize(output),
                }
            else:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["result"] = {"status": "error", "message": stderr.decode()[-300:]}
        except Exception as e:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["result"] = {"status": "error", "message": str(e)}

    background_tasks.add_task(_burn)
    return {"success": True, "task_id": task_id, "output": output}


# ── Translate ──

@router.post("/api/v1/subtitle/translate")
async def translate_subtitles(body: TranslateRequest):
    """Translate subtitles using Gemini AI."""
    try:
        from tubecli.extensions.cloud_api.extension import key_manager
        api_key = key_manager.get_active_key("gemini")
        if not api_key:
            raise HTTPException(400, "No Gemini API key available for translation.")

        import httpx
        texts = [s.get("text", "") for s in body.subtitles]
        batch_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))

        prompt = (
            f"Translate the following numbered lines to {body.target_language}. "
            f"Return ONLY a JSON array of translated strings in the same order.\n\n{batch_text}"
        )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192}
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            raise HTTPException(resp.status_code, f"Gemini error: {resp.text[:200]}")

        data = resp.json()
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])

        import re
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            translations = json.loads(json_match.group())
            result_subs = []
            for i, sub in enumerate(body.subtitles):
                new_sub = dict(sub)
                if i < len(translations):
                    new_sub["text"] = str(translations[i])
                result_subs.append(new_sub)
            return {"success": True, "subtitles": result_subs, "target": body.target_language}

        raise HTTPException(500, "Could not parse translation response")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Info ──

@router.get("/api/v1/subtitle/engines")
async def list_engines():
    """List available subtitle extraction engines."""
    engines = [
        {"id": "whisper", "name": "Whisper (Local AI)", "description": "OpenAI Whisper — chạy offline, miễn phí", "requires": "pip install openai-whisper"},
        {"id": "gemini", "name": "Gemini (Cloud AI)", "description": "Google Gemini — nhanh, dịch thuật, cần API key", "requires": "Gemini API key"},
        {"id": "youtube", "name": "YouTube CC", "description": "Tải CC có sẵn từ YouTube — không cần AI", "requires": "pip install yt-dlp"},
    ]
    return {"success": True, "engines": engines}


# ── Helpers ──

def _format_srt_time(seconds: float) -> str:
    """HH:MM:SS,mmm — rounded, never negative, hours kept to two digits.

    int((seconds % 1) * 1000) TRUNCATES, and binary floats sit just below the
    value you wrote: 1.4 came out 00:00:01,399 and 8.1 came out 00:00:08,099,
    so every cue was up to a millisecond early. Rounding whole milliseconds
    first also avoids emitting ",1000".

    A negative start is reachable from the editor, and it used to render
    -1:59:58,500 — syntactically invalid SRT that some players refuse to load
    the rest of the file after.
    """
    try:
        total_ms = int(round(max(0.0, float(seconds)) * 1000))
    except (TypeError, ValueError):
        total_ms = 0
    ms = total_ms % 1000
    total_s = total_ms // 1000
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _to_srt(subs: list) -> str:
    lines = []
    for i, sub in enumerate(subs, 1):
        start = _format_srt_time(sub.get("start", 0))
        end = _format_srt_time(sub.get("end", 0))
        lines.append(f"{i}\n{start} --> {end}\n{sub.get('text', '')}\n")
    return "\n".join(lines)


def _to_vtt(subs: list) -> str:
    lines = ["WEBVTT\n"]
    for sub in subs:
        start = _format_srt_time(sub.get("start", 0)).replace(",", ".")
        end = _format_srt_time(sub.get("end", 0)).replace(",", ".")
        lines.append(f"{start} --> {end}\n{sub.get('text', '')}\n")
    return "\n".join(lines)


def _to_ass(subs: list) -> str:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def stamp(value) -> str:
        """h:mm:ss.cc — rounded, matching assTime() in static/subtitle.js.

        int((s % 1) * 100) truncated, so 1.4 became .39 — and since the client
        rounds, the two exports of the same cue disagreed by a centisecond.
        """
        try:
            total_cs = int(round(max(0.0, float(value)) * 100))
        except (TypeError, ValueError):
            total_cs = 0
        cs = total_cs % 100
        total_s = total_cs // 100
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    events = []
    for sub in subs:
        start = stamp(sub.get("start", 0))
        end = stamp(sub.get("end", 0))
        text = str(sub.get("text", "")).replace("\r\n", "\n").replace("\n", "\\N")
        events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    return header + "\n".join(events) + "\n"
