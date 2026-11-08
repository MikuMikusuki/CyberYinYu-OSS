"""发情模式开关：按 QQ 记一个持久化布尔值，只影响私聊主聊天路径。

做法跟 ``memory.group_memory`` 的 ``memory_enabled`` 一致：坏文件不炸主流程、
原子写、读不到就当关着。状态文件很小，每次直接读盘，改完立刻生效（不需要
重启，也不依赖进程内缓存）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from time import time

logger = logging.getLogger("cyberyinyu")

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "heat_mode.json"

# 整条消息全匹配：聊天里提到「热模式」不触发，只认命令本身。
HEAT_ON_COMMAND = re.compile(r"^#(?:热模式|开启热模式|热模式开)\s*$")
HEAT_OFF_COMMAND = re.compile(r"^#(?:热模式结束|结束热模式|热模式关)\s*$")


def switch_request(text: str) -> str:
    """返回 ``"on"`` / ``"off"`` / ``""``（不是命令）。"""
    text = str(text or "").strip()
    if HEAT_ON_COMMAND.match(text):
        return "on"
    if HEAT_OFF_COMMAND.match(text):
        return "off"
    return ""


class HeatModeStore:
    """``data/heat_mode.json`` 的读写封装，只存「开着」的 QQ。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self._lock = threading.RLock()

    def _load(self) -> dict[str, dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            logger.warning("发情模式状态读取失败，按未开启处理: %s", exc)
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("发情模式状态文件损坏，按未开启处理: %s", exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("发情模式状态文件格式不对，按未开启处理")
            return {}
        clean: dict[str, dict] = {}
        for qq, entry in data.items():
            if isinstance(entry, bool):  # 兼容手写的 {"123": true}
                entry = {"active": entry}
            if isinstance(entry, dict) and entry.get("active") is True:
                clean[str(qq)] = dict(entry)
        return clean

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(f"{self.path.name}.tmp{os.getpid()}")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    def is_active(self, qq: str) -> bool:
        qq = str(qq or "").strip()
        if not qq:
            return False
        with self._lock:
            return qq in self._load()

    def set_active(self, qq: str, active: bool) -> bool:
        """返回是否真的改了状态；空 QQ、重复设置、写盘失败都返回 False。"""
        qq = str(qq or "").strip()
        if not qq:
            return False
        with self._lock:
            data = self._load()
            if active:
                if qq in data:
                    return False
                data[qq] = {
                    "active": True,
                    "since": int(time()),
                    "source": "命令:#热模式",
                }
            elif qq in data:
                data.pop(qq)
            else:
                return False
            try:
                self._save(data)
            except OSError:
                logger.exception("发情模式状态写盘失败: qq=%s", qq)
                return False
            return True
