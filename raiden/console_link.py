"""The host agent's link to the rAIden console.

`raiden run` on the database host is the host agent: it reads the PostgreSQL log and the operating system, which a
console that only speaks SQL cannot see. With RAIDEN_CONSOLE_URL, RAIDEN_CONSOLE_TOKEN and RAIDEN_SERVER_ID set (the
console shows the three lines when you create an agent token on the server's Settings tab), the daemon

  * sends a heartbeat every 30 s with host metrics from /proc (CPU, memory, load, disk I/O, network, storage of the
    data directory), so on-prem servers and VMs get the same charts as a managed service, and
  * forwards the incidents it handles on its own (crashes, corruption, a dropped table) with their tool calls,
    guardrail denials, escalations and outcome, so they appear on the console's incident board.

Nothing is sent when the variables are not set. A console that is down never slows the daemon: events wait in a
bounded queue and are retried.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger("raiden.link")

FORWARD = {"incident_opened", "agent_tool", "guard_denied", "escalation", "incident_closed"}
HEARTBEAT_S = 30.0
MAX_QUEUE = 500


def _read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


class HostMetrics:
    """Host metrics with the console's catalog names. Counters become rates between two samples."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._prev: dict[str, Any] | None = None

    @staticmethod
    def _cpu() -> tuple[float, float]:
        parts = _read("/proc/stat").split("\n", 1)[0].split()
        if len(parts) < 5 or parts[0] != "cpu":
            return 0.0, 0.0
        values = [float(x) for x in parts[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)  # idle + iowait
        return sum(values), idle

    def _disk(self) -> tuple[float, float, float, float, float] | None:
        """reads, sectors read, writes, sectors written, I/Os in flight: of the device holding the data directory if it
        can be found, otherwise of every whole disk."""
        device = None
        try:
            st = os.stat(self.data_dir)
            device = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
        except OSError:
            pass
        rows = [line.split() for line in _read("/proc/diskstats").splitlines() if len(line.split()) >= 14]
        if not rows:
            return None  # not Linux
        mine = [r for r in rows if device and f"{r[0]}:{r[1]}" == device]
        if not mine:  # device-mapper/LVM or a partition: fall back to whole disks (no trailing partition number)
            mine = [r for r in rows if not r[2].startswith(("loop", "ram", "dm-", "zram")) and not r[2][-1].isdigit()
                    or r[2].startswith("nvme") and "p" not in r[2][4:]]
        reads = sum(float(r[3]) for r in mine)
        sectors_read = sum(float(r[5]) for r in mine)
        writes = sum(float(r[7]) for r in mine)
        sectors_written = sum(float(r[9]) for r in mine)
        in_flight = sum(float(r[11]) for r in mine)
        return reads, sectors_read, writes, sectors_written, in_flight

    @staticmethod
    def _net() -> tuple[float, float] | None:
        text = _read("/proc/net/dev")
        if not text:
            return None
        rx = tx = 0.0
        for line in text.splitlines()[2:]:
            name, _, rest = line.partition(":")
            fields = rest.split()
            if name.strip() == "lo" or len(fields) < 9:
                continue
            rx += float(fields[0])
            tx += float(fields[8])
        return rx, tx

    def sample(self) -> dict[str, float]:
        now = time.monotonic()
        cur = {"t": now, "cpu": self._cpu(), "disk": self._disk(), "net": self._net()}
        out: dict[str, float] = {}
        mem = {k.strip(): float(v.split()[0]) * 1024 for k, v in (line.split(":", 1) for line in _read("/proc/meminfo").splitlines() if ":" in line)}
        if mem.get("MemTotal"):
            out["memory_percent"] = round(100.0 * (1 - mem.get("MemAvailable", mem.get("MemFree", 0.0)) / mem["MemTotal"]), 2)
        load = _read("/proc/loadavg").split()
        if load:
            out["load_average"] = float(load[0])
        try:
            vfs = os.statvfs(self.data_dir)
            total, free = vfs.f_blocks * vfs.f_frsize, vfs.f_bavail * vfs.f_frsize
            if total:
                out.update(storage_percent=round(100.0 * (total - free) / total, 2), storage_used=float(total - free), storage_free=float(free))
        except OSError:
            pass
        prev = self._prev
        self._prev = cur
        if prev is not None and now > prev["t"]:
            dt = now - prev["t"]
            total_d, idle_d = cur["cpu"][0] - prev["cpu"][0], cur["cpu"][1] - prev["cpu"][1]
            if total_d > 0:
                out["cpu_percent"] = round(100.0 * (1 - idle_d / total_d), 2)
            if cur["disk"] and prev["disk"]:
                r, sr, w, sw, inflight = cur["disk"]
                pr, psr, pw, psw, _ = prev["disk"]
                if r >= pr and w >= pw:  # counters reset when a device goes away
                    out.update(read_iops=round((r - pr) / dt, 2), write_iops=round((w - pw) / dt, 2), iops=round((r - pr + w - pw) / dt, 2),
                               read_throughput=round((sr - psr) * 512 / dt, 1), write_throughput=round((sw - psw) * 512 / dt, 1), disk_queue_depth=inflight)
            if cur["net"] and prev["net"]:
                rx, tx = cur["net"]
                if rx >= prev["net"][0] and tx >= prev["net"][1]:
                    out.update(network_bytes_ingress=rx - prev["net"][0], network_bytes_egress=tx - prev["net"][1])
        return out


class ConsoleLink:
    def __init__(self, url: str, token: str, server_id: str, data_dir: Path, model: str = "") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.server_id = server_id
        self.metrics = HostMetrics(data_dir)
        self.model = model
        self.pending: list[dict[str, Any]] = []
        self.wake = asyncio.Event()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sent = 0

    @classmethod
    def from_env(cls, data_dir: Path, model: str = "") -> ConsoleLink | None:
        url, token, sid = (os.environ.get(k, "").strip() for k in ("RAIDEN_CONSOLE_URL", "RAIDEN_CONSOLE_TOKEN", "RAIDEN_SERVER_ID"))
        if not (url and token and sid):
            return None
        return cls(url, token, sid, data_dir, model)

    # called by EventLog.emit, possibly from a worker thread
    def hook(self, record: dict[str, Any]) -> None:
        if record.get("type") not in FORWARD:
            return
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._enqueue, record)
        else:
            self._enqueue(record)

    def _enqueue(self, record: dict[str, Any]) -> None:
        self.pending.append(record)
        del self.pending[:-MAX_QUEUE]  # a console that is gone for hours costs memory, not the daemon
        self.wake.set()

    async def run(self) -> None:
        import httpx  # comes with the openai client the daemon already uses

        self.loop = asyncio.get_running_loop()
        headers = {"Authorization": f"Bearer {self.token}", "User-Agent": f"raiden-host-agent/{__version__}"}
        async with httpx.AsyncClient(base_url=self.url, headers=headers, timeout=10.0) as client:
            log.info("console link: reporting %s to %s", self.server_id, self.url)
            await asyncio.gather(self._heartbeats(client), self._events(client))

    async def _heartbeats(self, client: Any) -> None:
        await asyncio.to_thread(self.metrics.sample)  # the first sample only primes the counters
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                metrics = await asyncio.to_thread(self.metrics.sample)
                body = {"server_id": self.server_id, "metrics": metrics,
                        "agent": {"version": __version__, "hostname": socket.gethostname(), "model": self.model, "pid": os.getpid()}}
                r = await client.post("/api/v1/agents/heartbeat", json=body)
                if r.status_code == 401:
                    log.warning("console link: the console refused the agent token for %s (create a new one on the server's Settings tab)", self.server_id)
            except Exception as exc:  # noqa: BLE001 - the console being away must never stop the daemon
                log.debug("console link: heartbeat failed: %s", exc)

    async def _events(self, client: Any) -> None:
        backoff = 2.0
        while True:
            await self.wake.wait()
            self.wake.clear()
            await asyncio.sleep(1.0)  # a tool call rarely comes alone: send them together
            while self.pending:
                batch = self.pending[:50]
                try:
                    r = await client.post("/api/v1/agents/events", json={"server_id": self.server_id, "events": batch})
                    if r.status_code >= 500:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    del self.pending[: len(batch)]
                    self.sent += len(batch)
                    backoff = 2.0
                except Exception as exc:  # noqa: BLE001
                    log.debug("console link: events failed (%s), retry in %.0f s", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 120.0)
