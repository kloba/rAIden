"""Append-only JSONL event and audit logs (the raw material for the post-fight report)."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonlLog:
    """Thread-safe JSONL appender. Files are group-readable so the arena tooling can collect them."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
            try:
                os.write(fd, (line + "\n").encode())
            finally:
                os.close(fd)
        return record


class EventLog(JsonlLog):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.listeners: list[Callable[[dict[str, Any]], None]] = []  # e.g. the console link

    def emit(self, type_: str, **fields: Any) -> dict[str, Any]:
        record = self.write({"t": utcnow_iso(), "type": type_, **fields})
        for listener in self.listeners:
            try:
                listener(record)
            except Exception:  # a listener must never break the event log
                logging.getLogger("raiden").debug("event listener failed", exc_info=True)
        return record


class AuditLog(JsonlLog):
    def record(self, incident: str, tool: str, summary: str, decision: str, reason: str = "") -> dict[str, Any]:
        return self.write(
            {
                "t": utcnow_iso(),
                "incident": incident,
                "tool": tool,
                "summary": summary[:400],
                "decision": decision,
                "reason": reason,
            }
        )
