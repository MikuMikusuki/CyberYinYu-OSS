"""Turn model sticker placeholders into OneBot message segments."""

import json
import re
from pathlib import Path
from typing import Any


PERSONALITY_DIR = Path(__file__).resolve().parent
STICKER_DIR = PERSONALITY_DIR / "stickers"
STICKER_MAP = PERSONALITY_DIR / "stickers.json"
PLACEHOLDER = re.compile(r"\[\[([^\[\]\r\n]{1,24})\]\]")


def sticker_aliases() -> list[str]:
    try:
        data = json.loads(STICKER_MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    return [name for name, config in data.items() if isinstance(name, str) and _segment_for(name, config)]


def _segment_for(alias: str, config: Any) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    kind = config.get("type")
    if kind == "face" and str(config.get("id", "")).isdigit():
        return {"type": "face", "data": {"id": str(config["id"])}}
    if kind == "mface":
        emoji_id = config.get("emoji_id")
        package_id = config.get("emoji_package_id")
        if isinstance(emoji_id, str) and isinstance(package_id, str):
            data = {
                "emoji_id": emoji_id,
                "emoji_package_id": package_id,
                "summary": str(config.get("summary") or alias),
            }
            if isinstance(config.get("key"), str):
                data["key"] = config["key"]
            return {"type": "mface", "data": data}
    if kind == "image" and isinstance(config.get("file"), str):
        path = (STICKER_DIR / config["file"]).resolve()
        if path.parent == STICKER_DIR.resolve() and path.is_file():
            return {"type": "image", "data": {"file": str(path)}}
    return None


def render_stickers(message: str) -> str | list[dict[str, Any]]:
    """Convert [[alias]] tokens to image/face segments, leaving ordinary text intact."""
    try:
        mapping = json.loads(STICKER_MAP.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mapping = {}
    if not isinstance(mapping, dict):
        mapping = {}

    if message.strip() in mapping:
        exact_match = _segment_for(message.strip(), mapping[message.strip()])
        if exact_match is not None:
            return [exact_match]

    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in PLACEHOLDER.finditer(message):
        alias = match.group(1).strip()
        segment = _segment_for(alias, mapping.get(alias))
        before = PLACEHOLDER.sub("", message[cursor:match.start()])
        if before:
            segments.append({"type": "text", "data": {"text": before}})
        if segment is not None:
            segments.append(segment)
        cursor = match.end()

    if not segments:
        return PLACEHOLDER.sub("", message)
    remainder = PLACEHOLDER.sub("", message[cursor:])
    if remainder:
        segments.append({"type": "text", "data": {"text": remainder}})
    return segments
