"""Subtitle Extractor Nodes."""
import os
import importlib.util

_nodes_dir = os.path.dirname(os.path.abspath(__file__))


def _load(filename, classname):
    fp = os.path.join(_nodes_dir, filename)
    if not os.path.exists(fp):
        return None
    spec = importlib.util.spec_from_file_location(f"sub_node_{filename}", fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, classname, None)


SubtitleExtractNode = _load("subtitle_extract_node.py", "SubtitleExtractNode")

ALL_NODES = {}
if SubtitleExtractNode:
    ALL_NODES["subtitle_extract"] = SubtitleExtractNode
