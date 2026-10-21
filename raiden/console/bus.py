"""In-process event bus: background work publishes, the browser listens over Server-Sent Events."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any, Iterator


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, kind: str, **data: Any) -> None:
        msg = {"type": kind, "t": time.time(), **data}
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fan(msg)
        else:
            loop.call_soon_threadsafe(self._fan, msg)

    def _fan(self, msg: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # a slow browser misses ticks, never blocks the console

    @contextlib.contextmanager
    def listen(self) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        """A queue that receives every message while the block runs (a plain queue: a disconnecting browser can stop
        waiting on it at any moment, which an async generator does not allow)."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subs.add(q)
        try:
            yield q
        finally:
            self._subs.discard(q)

    @property
    def listeners(self) -> int:
        return len(self._subs)
