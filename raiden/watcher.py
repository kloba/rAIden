"""Tail PostgreSQL jsonlog files (rotation-safe) and probe liveness with pg_isready."""

from __future__ import annotations

import asyncio
import glob
import os
import time
from pathlib import Path
from typing import Callable

from .config import Config
from .incident import LogEvent, classify, parse_jsonlog_line


class LogTailer:
    """Follow the newest log file matching the glob; survives daily rotation and truncation."""

    def __init__(self, cfg: Config, from_start: bool = False, poll_s: float = 0.5) -> None:
        self.cfg = cfg
        self.from_start = from_start
        self.poll_s = poll_s
        self._path: Path | None = None
        self._offset = 0
        self._buf = b""
        self._primed = False

    def _latest(self) -> Path | None:
        files = sorted(glob.glob(str(self.cfg.pg_log_dir / self.cfg.pg_log_glob)))
        return Path(files[-1]) if files else None

    def _read_new(self) -> list[LogEvent]:
        out: list[LogEvent] = []
        assert self._path is not None
        try:
            size = self._path.stat().st_size
            if size < self._offset:  # truncated or replaced
                self._offset, self._buf = 0, b""
            if size == self._offset:
                return out
            with open(self._path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(2_000_000)
                self._offset += len(chunk)
        except (FileNotFoundError, PermissionError):
            return out
        data = self._buf + chunk
        *lines, self._buf = data.split(b"\n")
        for raw in lines:
            ev = parse_jsonlog_line(raw.decode("utf-8", "replace"))
            if ev:
                out.append(classify(ev))
        return out

    def poll_once(self) -> list[LogEvent]:
        events: list[LogEvent] = []
        latest = self._latest()
        if latest is None:
            return events
        if self._path is None:
            self._path = latest
            self._offset = 0 if self.from_start else latest.stat().st_size
            self._primed = True
        events += self._read_new()
        if latest != self._path:  # a newer file appeared: finish the old one, then switch
            events += self._read_new()
            self._path, self._offset, self._buf = latest, 0, b""
            events += self._read_new()
        return events

    async def run(self, queue: "asyncio.Queue[LogEvent]") -> None:
        while True:
            for ev in await asyncio.to_thread(self.poll_once):
                await queue.put(ev)
            await asyncio.sleep(self.poll_s)


class LivenessMonitor:
    """pg_isready every few seconds; reports sustained outages and flapping as synthetic critical events."""

    def __init__(self, cfg: Config, emit: Callable[..., object]) -> None:
        self.cfg = cfg
        self.emit = emit
        self.up = True
        self.down_since: float | None = None
        self.reported_down = False
        self.transitions: list[float] = []
        self.last_flap_report = 0.0

    async def _ready(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "pg_isready", "-q", "-t", "2", "-h", "/var/run/postgresql",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "PATH": self.cfg.path_env},
        )
        return (await proc.wait()) == 0

    def _synthetic(self, kind: str, message: str) -> LogEvent:
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".000Z"
        ev = LogEvent(ts=now, iso=iso, severity="LOG", sqlstate="", message=message, backend_type="raiden-liveness")
        ev.kind, ev.weight, ev.signature = kind, 100, f"{kind}:{message[:60]}"
        return ev

    async def run(self, queue: "asyncio.Queue[LogEvent]") -> None:
        while True:
            try:
                ok = await self._ready()
            except FileNotFoundError:
                ok = False
            await self.step(ok, time.monotonic(), queue)
            await asyncio.sleep(self.cfg.liveness_interval_s)

    async def step(self, ok: bool, now: float, queue: "asyncio.Queue[LogEvent]") -> None:
        # only flips of the last 2 minutes count: a database that is stable again must stop being "flapping"
        self.transitions = [t for t in self.transitions if now - t < 120]
        if ok != self.up:
            self.transitions.append(now)
            self.up = ok
            if ok:
                if self.reported_down and self.down_since is not None:
                    self.emit("liveness_up", outage_s=round(now - self.down_since, 1))
                self.down_since, self.reported_down = None, False
            else:
                self.down_since = now
        if not self.up and self.down_since and not self.reported_down:
            if now - self.down_since >= self.cfg.liveness_down_after_s:
                self.reported_down = True
                self.emit("liveness_down", seconds=round(now - self.down_since, 1))
                await queue.put(self._synthetic("service_down", f"PostgreSQL not accepting connections for {int(now - self.down_since)}s"))
        if len(self.transitions) >= 6 and now - self.last_flap_report > 120:
            self.last_flap_report = now
            self.emit("liveness_flapping", transitions=len(self.transitions))
            await queue.put(self._synthetic("flapping", f"PostgreSQL availability flapping ({len(self.transitions)} transitions in 2 min)"))
