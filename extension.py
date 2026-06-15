"""
Subtitle Extractor Extension — AI-powered subtitle extraction for TubeCLI.
Supports: Whisper (local), Gemini (cloud), YouTube CC (yt-dlp).
"""
import os
import logging
from tubecli.core.extension_manager import Extension

logger = logging.getLogger("SubtitleExtractor")


class SubtitleExtractorExtension(Extension):
    name = "subtitle_extractor"
    version = "2026.06.09.102310"
    description = "AI Subtitle Extraction — Whisper, Gemini, YouTube CC"
    author = "TubeCreate"
    extension_type = "external"

    def on_enable(self):
        logger.info("Subtitle Extractor extension enabled")
        self._ensure_dependencies()
        self._register_skill()

    def _ensure_dependencies(self):
        try:
            import whisper
            import yt_dlp
        except ImportError:
            logger.info("Installing missing dependencies for Subtitle Extractor...")
            import subprocess
            import sys
            req_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
            if os.path.exists(req_file):
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req_file])
                    logger.info("Successfully installed requirements.")
                except Exception as e:
                    logger.error(f"Failed to install requirements: {e}")

    def _register_skill(self):
        """Register subtitle extraction skill for chatbot routing."""
        try:
            from tubecli.core.skill import skill_manager
            from tubecli.config import get_language

            existing = skill_manager.find_by_name("Subtitle Extractor")
            lang = get_language()

            if lang == "vi":
                desc = "Tách phụ đề từ video/audio bằng Whisper AI, Gemini Cloud, hoặc YouTube CC. Hỗ trợ dịch thuật, export SRT/VTT/ASS, ghi sub vào video."
                cmds = [
                    "tách sub", "tách phụ đề", "extract subtitle", "lấy sub",
                    "phụ đề", "subtitle", "caption", "transcribe",
                ]
                sop = (
                    "1. Nhận file video/audio hoặc URL YouTube\n"
                    "2. Chọn engine: whisper (local), gemini (cloud), youtube (CC)\n"
                    "3. Tách phụ đề → trả về SRT file\n"
                    "4. Nếu yêu cầu, dịch sang ngôn ngữ khác\n"
                    "5. Nếu yêu cầu, burn subtitle vào video bằng FFmpeg"
                )
            else:
                desc = "Extract subtitles from video/audio using Whisper AI, Gemini Cloud, or YouTube CC. Supports translation, SRT/VTT/ASS export, and burning subtitles to video."
                cmds = [
                    "extract subtitle", "get sub", "transcribe video", "transcribe audio",
                    "subtitle", "caption", "transcribe", "generate srt",
                ]
                sop = (
                    "1. Receive a video/audio file or YouTube URL\n"
                    "2. Choose engine: whisper (local), gemini (cloud), youtube (CC)\n"
                    "3. Extract subtitles → return SRT file\n"
                    "4. If requested, translate to another language\n"
                    "5. If requested, burn subtitles into video using FFmpeg"
                )

            if not existing:
                skill_manager.create(
                    name="Subtitle Extractor",
                    description=desc,
                    skill_type="Extension Skill",
                    commands=cmds,
                    workflow_data={
                        "extension": "subtitle_extractor",
                        "action": "extract_subtitle",
                        "sop": sop,
                    },
                )
                logger.info("✅ Subtitle Extractor skill registered successfully.")
            else:
                skill_manager.update(
                    existing.id,
                    description=desc,
                    commands=cmds,
                    workflow_data={
                        "extension": "subtitle_extractor",
                        "action": "extract_subtitle",
                        "sop": sop,
                    },
                )
                logger.info("⚡ Subtitle Extractor skill updated/synced.")
        except Exception as e:
            logger.warning(f"Could not register subtitle skill: {e}")

    def get_routes(self):
        try:
            import importlib.util
            routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subtitle_routes.py")
            spec = importlib.util.spec_from_file_location("subtitle_ext_routes", routes_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            router = getattr(mod, "router", None)
            logger.info(f"Subtitle Extractor: loaded router, {len(router.routes) if router else 0} routes")
            return router
        except Exception as e:
            logger.error(f"Failed to load subtitle routes: {e}")
            import traceback; traceback.print_exc()
            return None

    def get_nodes(self):
        try:
            import importlib.util
            nodes_init = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nodes", "__init__.py")
            spec = importlib.util.spec_from_file_location("subtitle_nodes", nodes_init)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return getattr(mod, "ALL_NODES", {})
        except Exception as e:
            logger.warning(f"Could not load subtitle nodes: {e}")
            return {}

    def get_telegram_actions(self):
        return {
            "extract_subtitle": self._action_extract_subtitle,
        }

    async def _action_extract_subtitle(self, action_data: dict, context: dict) -> str:
        """Telegram action: extract subtitles from a file or URL."""
        file_path = action_data.get("file_path", "")
        url = action_data.get("url", "")
        engine = action_data.get("engine", "whisper")
        language = action_data.get("language", None)

        if not file_path and not url:
            return "❌ Thiếu file_path hoặc url. Hãy chỉ định nguồn video/audio."

        try:
            import importlib.util
            engines_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engines")

            if url and ("youtube.com" in url or "youtu.be" in url):
                engine = "youtube"

            if engine == "youtube":
                spec = importlib.util.spec_from_file_location("yt_engine", os.path.join(engines_dir, "youtube_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_youtube(url, languages=[language] if language else None)
            elif engine == "gemini":
                spec = importlib.util.spec_from_file_location("gem_engine", os.path.join(engines_dir, "gemini_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_gemini(file_path, language=language)
            else:
                spec = importlib.util.spec_from_file_location("wh_engine", os.path.join(engines_dir, "whisper_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_whisper(file_path, language=language)

            if result.get("status") == "success":
                subs = result.get("subtitles", [])
                count = len(subs)
                lang = result.get("language", "?")
                duration = result.get("duration", 0)

                # Save SRT file
                srt_path = file_path.rsplit(".", 1)[0] + ".srt" if file_path else "subtitles.srt"
                srt_content = self._to_srt(subs)
                with open(srt_path, "w", encoding="utf-8") as f:
                    f.write(srt_content)

                return (
                    f"✅ **Tách phụ đề thành công!**\n\n"
                    f"🎯 Engine: {engine.upper()}\n"
                    f"🌐 Ngôn ngữ: {lang}\n"
                    f"📊 {count} dòng phụ đề | ⏱️ {duration:.0f}s\n"
                    f"📁 File: `{os.path.basename(srt_path)}`\n\n"
                    f"📝 Preview:\n"
                    + "\n".join(f"  {s['start']:.1f}s → {s['end']:.1f}s: {s['text'][:60]}" for s in subs[:5])
                    + (f"\n  ... +{count - 5} dòng nữa" if count > 5 else "")
                )
            else:
                return f"❌ Tách subtitle thất bại: {result.get('message', 'Unknown error')}"

        except Exception as e:
            logger.error(f"Subtitle extraction error: {e}")
            return f"❌ Lỗi tách subtitle: {str(e)[:200]}"

    @staticmethod
    def _to_srt(subtitles: list) -> str:
        """Convert subtitle list to SRT format."""
        lines = []
        for i, sub in enumerate(subtitles, 1):
            start = _format_srt_time(sub.get("start", 0))
            end = _format_srt_time(sub.get("end", 0))
            text = sub.get("text", "")
            lines.append(f"{i}\n{start} --> {end}\n{text}\n")
        return "\n".join(lines)


def _format_srt_time(seconds: float) -> str:
    """Format seconds to SRT timestamp: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
