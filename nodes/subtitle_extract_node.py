"""
Subtitle Extract Node — TubeCLI Workflow Node for subtitle extraction.
Registers as "subtitle_extract" in the node registry.
"""
import os
import logging
from tubecli.nodes.base_node import BaseNode, NodePort, PortType

logger = logging.getLogger("SubtitleExtractor.Node")


class SubtitleExtractNode(BaseNode):
    display_name = "📝 Subtitle Extract"
    description = "Extract subtitles from video/audio using Whisper, Gemini, or YouTube CC"
    category = "AI"
    node_type = "subtitle_extract"

    def __init__(self):
        super().__init__()
        self.inputs = [
            NodePort("file_path", PortType.TEXT, "Path to video/audio file", required=False),
            NodePort("url", PortType.TEXT, "YouTube URL (for CC extraction)", required=False),
            NodePort("engine", PortType.TEXT, "Engine: whisper, gemini, youtube", required=False),
            NodePort("language", PortType.TEXT, "Language code (e.g., vi, en, zh)", required=False),
        ]
        self.outputs = [
            NodePort("subtitles", PortType.TEXT, "JSON array of subtitles"),
            NodePort("srt", PortType.TEXT, "SRT formatted text"),
            NodePort("count", PortType.TEXT, "Number of subtitle entries"),
        ]

    async def execute(self, inputs: dict) -> dict:
        file_path = inputs.get("file_path", "") or self.config.get("file_path", "")
        url = inputs.get("url", "") or self.config.get("url", "")
        engine = inputs.get("engine", "") or self.config.get("engine", "whisper")
        language = inputs.get("language", "") or self.config.get("language", None)

        import importlib.util
        engines_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engines")

        try:
            if url and ("youtube.com" in url or "youtu.be" in url):
                spec = importlib.util.spec_from_file_location("yt", os.path.join(engines_dir, "youtube_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_youtube(url, languages=[language] if language else None)
            elif engine == "gemini":
                spec = importlib.util.spec_from_file_location("gem", os.path.join(engines_dir, "gemini_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_gemini(file_path, language=language)
            else:
                spec = importlib.util.spec_from_file_location("wh", os.path.join(engines_dir, "whisper_engine.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                result = await mod.extract_whisper(file_path, language=language)

            if result.get("status") == "success":
                subs = result.get("subtitles", [])
                import json
                srt_lines = []
                for i, s in enumerate(subs, 1):
                    start = self._fmt(s["start"])
                    end = self._fmt(s["end"])
                    srt_lines.append(f"{i}\n{start} --> {end}\n{s['text']}\n")

                return {
                    "subtitles": json.dumps(subs, ensure_ascii=False),
                    "srt": "\n".join(srt_lines),
                    "count": str(len(subs)),
                }
            else:
                return {"subtitles": "[]", "srt": "", "count": "0"}

        except Exception as e:
            logger.error(f"SubtitleExtractNode error: {e}")
            return {"subtitles": "[]", "srt": "", "count": "0"}

    @staticmethod
    def _fmt(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        ms = int((seconds % 1) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
