"""
YouTube Engine — Extract subtitles/captions from YouTube.
Primary: youtube-transcript-api (fast, no subprocess needed)
Fallback: yt-dlp (for edge cases where transcript API fails)
"""
import os
import re
import json
import asyncio
import logging
import tempfile
from typing import Optional, List

logger = logging.getLogger("SubtitleExtractor.YouTube")


def _extract_video_id(url: str) -> Optional[str]:
    """Extract video ID from various YouTube URL formats."""
    patterns = [
        r'(?:v=|/v/|youtu\.be/|/embed/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


# ═══════════════════════════════════════════════════════════════
# Primary Method: youtube-transcript-api (v1.2+)
# ═══════════════════════════════════════════════════════════════

def _extract_via_transcript_api_sync(video_id: str, languages: Optional[List[str]] = None) -> dict:
    """
    Extract subtitles using youtube-transcript-api (v1.2+ instance API).
    Much faster and lighter than yt-dlp — no subprocess needed.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return {"status": "fallback", "message": "youtube-transcript-api chưa cài."}

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)

        # Collect available languages info
        available_manual = []
        available_auto = []
        transcripts = []  # store actual Transcript objects for later fetch
        for t in transcript_list:
            info = {"code": t.language_code, "name": t.language}
            if t.is_generated:
                available_auto.append(info)
            else:
                available_manual.append(info)
            transcripts.append(t)

        logger.info(f"Transcript API — Manual: {[l['code'] for l in available_manual]}, Auto: {len(available_auto)} langs")

        if not transcripts:
            return {"status": "error", "message": "Video này không có phụ đề (CC). Thử dùng Whisper hoặc Gemini."}

        # Determine which transcript to fetch
        # Priority: manual > auto, with language preference
        target_langs = []
        if languages:
            target_langs.extend(languages)
        target_langs.extend(["ja", "en", "ko", "zh-Hans", "zh-Hant", "vi"])

        chosen = None

        # Try manual transcripts first
        for lang in target_langs:
            for t in transcripts:
                if not t.is_generated and t.language_code == lang:
                    chosen = t
                    break
            if chosen:
                break

        # Fall back to auto-generated
        if chosen is None:
            for lang in target_langs:
                for t in transcripts:
                    if t.is_generated and t.language_code == lang:
                        chosen = t
                        break
                if chosen:
                    break

        # Last resort: take the first available
        if chosen is None:
            chosen = transcripts[0]

        logger.info(f"Fetching transcript: {chosen.language_code} ({chosen.language}), auto={chosen.is_generated}")
        fetched = chosen.fetch()
        raw_data = fetched.to_raw_data()

        # Convert to our standard format
        subtitles = []
        for entry in raw_data:
            text = entry.get("text", "").strip()
            if not text:
                continue
            text = re.sub(r'<[^>]+>', '', text).strip()
            if not text:
                continue

            start = round(entry.get("start", 0), 3)
            duration = entry.get("duration", 0) or 0
            end = round(start + duration, 3)
            subtitles.append({"start": start, "end": end, "text": text})

        if not subtitles:
            return {"status": "error", "message": "Phụ đề trống hoặc không hợp lệ."}

        duration = subtitles[-1]["end"] if subtitles else 0
        all_langs = sorted(set(
            [l["code"] for l in available_manual] +
            [l["code"] for l in available_auto]
        ))

        return {
            "status": "success",
            "subtitles": subtitles,
            "language": chosen.language_code,
            "original_language": chosen.language_code,
            "duration": duration,
            "engine": "youtube_transcript_api",
            "count": len(subtitles),
            "title": "",  # will be filled by caller
            "is_auto_generated": chosen.is_generated,
            "available_languages": all_langs,
        }

    except Exception as e:
        error_msg = str(e)
        logger.warning(f"youtube-transcript-api failed: {error_msg[:300]}")
        if "disabled" in error_msg.lower() or "no transcript" in error_msg.lower():
            return {"status": "error", "message": "Video này đã tắt phụ đề. Thử dùng Whisper hoặc Gemini."}
        return {"status": "fallback", "message": f"Transcript API: {error_msg[:200]}"}


async def _get_video_title(video_id: str) -> str:
    """Get video title using yt-dlp (lightweight call)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "yt-dlp", "--print", "title", "--skip-download", "--no-warnings",
            f"https://www.youtube.com/watch?v={video_id}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
# Fallback Method: yt-dlp
# ═══════════════════════════════════════════════════════════════

def _parse_srt(srt_text: str) -> List[dict]:
    """Parse SRT text into subtitle entries."""
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    subtitles = []
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 2:
            continue
        time_line = None
        text_lines = []
        for i, line in enumerate(lines):
            if '-->' in line:
                time_line = line
                text_lines = lines[i + 1:]
                break
        if not time_line:
            continue
        parts = time_line.split('-->')
        if len(parts) != 2:
            continue
        start = _srt_time_to_seconds(parts[0].strip())
        end = _srt_time_to_seconds(parts[1].strip())
        text = '\n'.join(text_lines).strip()
        text = re.sub(r'<[^>]+>', '', text).strip()
        if text:
            subtitles.append({"start": round(start, 3), "end": round(end, 3), "text": text})
    return subtitles


def _srt_time_to_seconds(timestamp: str) -> float:
    """Convert SRT timestamp (HH:MM:SS,mmm) to seconds."""
    timestamp = timestamp.replace(',', '.')
    parts = timestamp.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(timestamp)


async def _extract_via_ytdlp(url: str, languages: Optional[List[str]] = None) -> dict:
    """Fallback extraction using yt-dlp."""
    tmp_dir = tempfile.mkdtemp(prefix="yt_subtitle_")
    try:
        # Step 1: Get video info
        detect_cmd = ["yt-dlp", "--dump-json", "--skip-download", "--no-warnings", url]
        logger.info(f"yt-dlp fallback: detecting subtitles for {url}")
        detect_proc = await asyncio.create_subprocess_exec(
            *detect_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        detect_stdout, detect_stderr = await asyncio.wait_for(detect_proc.communicate(), timeout=90)

        try:
            raw_text = detect_stdout.decode("utf-8", errors="replace")
            json_start = raw_text.find('{')
            if json_start > 0:
                raw_text = raw_text[json_start:]
            info_data = json.loads(raw_text)
        except Exception as e:
            err_msg = detect_stderr.decode("utf-8", errors="replace")
            logger.error(f"yt-dlp JSON parse failed: {e}. Stderr: {err_msg[:500]}")
            return {"status": "error", "message": f"yt-dlp lỗi phân tích: {str(e)[:100]}"}

        original_language = info_data.get("language")
        available_langs = set(info_data.get("subtitles", {}).keys())
        available_auto_langs = set(info_data.get("automatic_captions", {}).keys())
        title = info_data.get("title", "") or info_data.get("fulltitle", "")

        # Step 2: Find best language
        target_langs = []
        if original_language:
            target_langs.append(original_language)
        if languages:
            target_langs.extend(languages)
        target_langs.append("en")

        best_lang = None
        for tl in target_langs:
            if tl in available_langs:
                best_lang = tl
                break
            if tl in available_auto_langs:
                best_lang = tl
                break
        if not best_lang:
            if available_langs:
                best_lang = next(iter(available_langs))
            elif available_auto_langs:
                best_lang = next(iter(available_auto_langs))

        # Step 3: Download subtitles
        sub_lang_arg = best_lang if best_lang else "all"
        cmd = [
            "yt-dlp", "--skip-download", "--write-sub", "--write-auto-sub",
            "--sub-format", "srt", "--sub-langs", sub_lang_arg, "--no-warnings",
            "-o", os.path.join(tmp_dir, "%(id)s.%(ext)s"), url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            err_msg = stderr.decode()[:300]
            if "no subtitles" in err_msg.lower():
                return {"status": "error", "message": "Video này không có phụ đề (CC)."}
            return {"status": "error", "message": f"yt-dlp error: {err_msg}"}

        srt_files = [os.path.join(tmp_dir, f) for f in os.listdir(tmp_dir) if f.endswith(".srt")]
        if not srt_files:
            return {"status": "error", "message": "Không tìm thấy file phụ đề."}

        # Pick best SRT
        best_srt = srt_files[0]
        detected_lang = "unknown"
        best_score = -9999
        for srt_path in srt_files:
            fname = os.path.basename(srt_path).lower()
            lang_match = re.search(r'\.([a-z]{2,3}(-[a-z0-9]+)?)\.srt$', fname)
            lang = lang_match.group(1) if lang_match else "unknown"
            score = 0
            if original_language and lang == original_language:
                score += 2000
            if languages and lang in languages:
                score += 1000
            elif lang == "en":
                score += 500
            if "auto" in fname:
                score -= 50
            if score > best_score:
                best_score = score
                best_srt = srt_path
                detected_lang = lang

        with open(best_srt, "r", encoding="utf-8") as f:
            srt_content = f.read()

        subtitles = _parse_srt(srt_content)
        duration = subtitles[-1]["end"] if subtitles else 0

        return {
            "status": "success",
            "subtitles": subtitles,
            "language": detected_lang,
            "original_language": original_language or detected_lang,
            "duration": duration,
            "engine": "yt-dlp",
            "count": len(subtitles),
            "title": title,
            "available_languages": sorted(list(available_langs | available_auto_langs)),
        }
    except asyncio.TimeoutError:
        return {"status": "error", "message": "yt-dlp timeout (>90s)."}
    except FileNotFoundError:
        return {"status": "error", "message": "yt-dlp chưa cài. Chạy: pip install yt-dlp"}
    except Exception as e:
        logger.error(f"yt-dlp subtitle error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        import shutil
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
# Main Entry Point
# ═══════════════════════════════════════════════════════════════

async def extract_youtube(
    url: str,
    languages: Optional[List[str]] = None,
) -> dict:
    """
    Extract subtitles from a YouTube video.
    
    Strategy:
    1. Try youtube-transcript-api first (fast, no subprocess)
    2. Fall back to yt-dlp if transcript API fails
    """
    if not url:
        return {"status": "error", "message": "URL không được để trống."}

    video_id = _extract_video_id(url)
    if not video_id:
        return {"status": "error", "message": f"Không thể trích xuất video ID từ URL: {url}"}

    # ── Method 1: youtube-transcript-api (sync, run in thread) ──
    logger.info(f"[CC] Trying youtube-transcript-api for: {video_id}")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _extract_via_transcript_api_sync, video_id, languages)

    if result.get("status") == "success":
        # Get title if missing
        if not result.get("title"):
            result["title"] = await _get_video_title(video_id)
        logger.info(f"✅ Transcript API success: {result.get('count')} entries, lang={result.get('language')}")
        return result

    if result.get("status") == "error":
        # Hard error (no subtitles, disabled) — don't try fallback
        logger.warning(f"Transcript API hard error: {result.get('message')}")
        return result

    # ── Method 2: yt-dlp fallback ──
    fallback_msg = result.get("message", "")
    logger.info(f"[CC] Transcript API returned fallback ({fallback_msg[:100]}), trying yt-dlp...")

    ytdlp_result = await _extract_via_ytdlp(url, languages)
    if ytdlp_result.get("status") == "success":
        logger.info(f"✅ yt-dlp fallback success: {ytdlp_result.get('count')} entries")
        return ytdlp_result

    # Both failed
    return {
        "status": "error",
        "message": f"Cả 2 phương pháp đều thất bại.\n• Transcript API: {fallback_msg}\n• yt-dlp: {ytdlp_result.get('message', 'unknown')}"
    }
