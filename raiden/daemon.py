"""The 24/7 daemon: log tailer + liveness probe -> incident aggregator -> agent session -> verify -> evolve."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from .agent import SessionResult, run_session
from .config import Config
from .events import AuditLog, EventLog, utcnow_iso
from .guardrails import Guard
from .incident import CRITICAL_WEIGHT, Aggregator, Incident, LogEvent
from .llm import AzureLLM
from .memory import snapshot, validate_changes
from .patrol import log_patrol, patrol_incident, run_checks, worst
from .prompts import evolve_prompt, incident_prompt, system_prompt
from .tools import ToolBox
from .watcher import LivenessMonitor, LogTailer

log = logging.getLogger("raiden")


class Daemon:
    def __init__(self, cfg: Config, from_start: bool = False) -> None:
        self.cfg = cfg
        self.events = EventLog(cfg.events_path)
        self.audit = AuditLog(cfg.audit_path)
        self.guard = Guard(cfg)
        self.llm = AzureLLM(cfg)
        self.agg = Aggregator(cfg.trigger_weight, cfg.quiet_s, cfg.max_wait_s, cfg.suppress_s)
        self.queue: asyncio.Queue[LogEvent] = asyncio.Queue()
        self.tailer = LogTailer(cfg, from_start=from_start)
        self.liveness = LivenessMonitor(cfg, self.events.emit)
        self.busy = False
        self.recurrence_hint: str | None = None
        self.finish_epoch = 0.0
        self.handled_sigs: set[str] = set()
        self.handled_kinds: set[str] = set()
        self.verifying = False
        self.post_finish: list[LogEvent] = []
        self._last_recovery_event = 0.0
        self._patrol_until: dict[str, float] = {}
        self._patrol_now = asyncio.Event()  # set when the log says the configuration was reloaded
        self._admin_until = 0.0  # monotonic deadline: clients disconnected by a planned shutdown are not an incident

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        for d in (self.cfg.incidents_dir, self.cfg.work_dir, self.cfg.memory_dir, self.cfg.escalations_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.events.emit("daemon_started", model=self.cfg.deployment, version="0.1.0")
        log.info("rAIden is on duty: model=%s, watching %s/%s", self.cfg.deployment, self.cfg.pg_log_dir, self.cfg.pg_log_glob)
        await asyncio.gather(
            self.tailer.run(self.queue), self.liveness.run(self.queue), self._consume(), self._ticker(), self._patrol_loop()
        )

    async def _consume(self) -> None:
        while True:
            ev = await self.queue.get()
            if ev.kind == "config_reload":
                self._patrol_now.set()  # a config change is exactly when a pending-restart time bomb appears
                continue
            if ev.kind == "admin_shutdown":
                self._admin_until = time.monotonic() + 45.0
                continue
            if ev.kind in ("fatal_admin", "recovery_mode") and time.monotonic() < self._admin_until:
                continue  # the clients of a planned restart; a hung shutdown outlasts this window and is still caught
            if self.finish_epoch and ev.ts <= self.finish_epoch and (ev.weight < CRITICAL_WEIGHT or ev.signature in self.handled_sigs or ev.kind in self.handled_kinds):
                continue  # echo of something the agent already lived through (or noise from its own restart)
            if self.verifying and ev.weight >= CRITICAL_WEIGHT and ev.ts > self.finish_epoch:
                self.post_finish.append(ev)
            if ev.kind == "crash" and "not properly shut down" in ev.message and time.monotonic() - self._last_recovery_event > 0.5:
                self._last_recovery_event = time.monotonic()
                self.events.emit("crash_recovery_seen", at=ev.iso)
            self.agg.feed(ev)

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.busy:
                continue
            inc = self.agg.poll(recurrence_of=self.recurrence_hint)
            if inc:
                self.recurrence_hint = None
                asyncio.create_task(self.handle(inc))

    async def _patrol_loop(self) -> None:
        if self.cfg.patrol_interval_s <= 0:
            return
        await asyncio.sleep(60)
        while True:
            self._patrol_now.clear()  # a reload logged while this patrol runs (or is rate-limited) is still seen next round
            started = time.monotonic()
            await self._patrol_once()
            try:  # the next patrol: on schedule, or as soon as the log shows a configuration reload
                await asyncio.wait_for(self._patrol_now.wait(), timeout=self.cfg.patrol_interval_s)
                self.events.emit("patrol_triggered", reason="config_reload")
                await asyncio.sleep(3)  # PostgreSQL logs the reload result as a burst
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(max(0.0, self.cfg.patrol_min_gap_s - (time.monotonic() - started)))

    async def _patrol_once(self) -> None:
        results = await asyncio.to_thread(run_checks, self.cfg)
        log_patrol(self.cfg, results)
        self.events.emit("patrol_run", worst=worst(results), results={r["name"]: r["status"] for r in results})
        inc = patrol_incident(results)
        if inc and not self.busy:
            now = time.monotonic()
            if any(self._patrol_until.get(sig, 0.0) <= now for sig in inc.signature_set):
                for sig in inc.signature_set:
                    self._patrol_until[sig] = now + 900
                self.busy = True
                asyncio.create_task(self.handle(inc))

    # ------------------------------------------------------------------ one incident
    def _liveness_text(self) -> str:
        if self.liveness.up:
            return "PostgreSQL is accepting connections"
        since = self.liveness.down_since
        return f"PostgreSQL is NOT accepting connections (for {int(time.monotonic() - since)} s)" if since else "PostgreSQL is NOT accepting connections"

    @staticmethod
    def _write_meta(path: Path, meta: dict) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, default=str))
        tmp.replace(path)

    async def handle(self, inc: Incident) -> SessionResult | None:
        self.busy = True
        outcome, recurred = "error", False
        try:
            res, outcome, idir = await self._run_incident(inc)
            good = outcome in ("resolved", "mitigated")
            if good and set(inc.kinds) - {"patrol"}:
                recurred = await self._verify(inc)
            if good and inc.critical and self.cfg.evolve_enabled and not recurred:
                await self._evolve(inc, idir)
            return res
        finally:
            self.verifying = False
            await self._acknowledge_patrol()
            self.events.emit("incident_settled", id=inc.id, outcome=outcome, recurred=recurred)
            self.busy = False

    async def _acknowledge_patrol(self) -> None:
        """Whatever patrol still reports right after an incident was just looked at (a 30-minute 'crashes in the
        last 30 min' check keeps firing for a storm the agent already handled). Re-notify only after a cooldown."""
        try:
            results = await asyncio.to_thread(run_checks, self.cfg)
        except Exception:
            return
        until = time.monotonic() + 1800
        for r in results:
            if r["status"] != "OK":
                self._patrol_until[f"patrol:{r['name']}"] = until

    async def _run_incident(self, inc: Incident) -> tuple[SessionResult | None, str, Path]:
        idir = self.cfg.incidents_dir / inc.id
        outcome = "error"
        res: SessionResult | None = None
        try:
            idir.mkdir(parents=True, exist_ok=True)
            meta = {
                "id": inc.id, "opened_at": inc.opened_iso, "dispatched_at": utcnow_iso(), "reason": inc.reason,
                "signatures": sorted(inc.signature_set), "kinds": inc.kinds, "events_count": inc.events_total,
                "critical": inc.critical, "recurrence_of": inc.recurrence_of, "model": self.cfg.deployment,
            }
            self._write_meta(idir / "incident.json", meta)
            (idir / "incident.md").write_text(inc.prompt_block() + "\n")
            self.events.emit("incident_opened", id=inc.id, reason=inc.reason, kinds=inc.kinds, events=inc.events_total, critical=inc.critical, recurrence_of=inc.recurrence_of)
            self.events.emit("incident_dispatched", id=inc.id)
            toolbox = ToolBox(self.cfg, self.guard, self.audit, self.events, inc.id, idir)
            res = await run_session(
                self.cfg, self.llm, toolbox, system_prompt(self.cfg), incident_prompt(self.cfg, inc, self._liveness_text(), idir),
                max_turns=self.cfg.max_turns, timeout_s=self.cfg.incident_timeout_s,
                transcript_path=idir / "transcript.jsonl", events=self.events, kind="incident",
            )
            outcome = res.resolution or ("timeout" if res.timed_out else "error")
            meta.update(
                finished_at=utcnow_iso(), outcome=outcome, summary=res.summary, session_id=inc.id, num_turns=res.turns,
                duration_ms=int(res.duration_s * 1000), total_cost_usd=res.cost_usd, tool_calls=res.tool_calls,
                denied_calls=res.denied_calls, report_path=str(idir / "report.md"), error=res.error,
                usage={"prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens, "cached_tokens": res.cached_tokens},
            )
            self._write_meta(idir / "incident.json", meta)
            self.events.emit("incident_closed", id=inc.id, outcome=outcome, turns=res.turns, seconds=round(res.duration_s, 1), tool_calls=res.tool_calls, denied=res.denied_calls, summary=res.summary[:300])
        except Exception as exc:
            log.exception("incident handling failed")
            self.events.emit("incident_error", id=inc.id, error=f"{exc.__class__.__name__}: {exc}")
        finally:
            self.finish_epoch = time.time()
            self.handled_sigs = inc.signature_set
            self.handled_kinds = set(inc.kinds) - {"patrol", "manual"}  # the agent had the whole session to look at these
            self.post_finish.clear()
            self.verifying = True
            stale_kinds = ({"service_down", "flapping"} if self.liveness.up else set()) | self.handled_kinds
            self.agg.discard_open(inc.signature_set, drop_noncritical=True, drop_kinds=stale_kinds)
            if outcome in ("resolved", "mitigated"):
                self.agg.mute(inc)
        return res, outcome, idir

    async def _verify(self, inc: Incident) -> bool:
        """Stay quiet for the verification window; a repeat of the same critical kinds means the fix did not hold."""
        end = time.monotonic() + self.cfg.verify_window_s
        while time.monotonic() < end:
            await asyncio.sleep(1.0)
            if any(e.kind in inc.kinds for e in self.post_finish):
                self.events.emit("incident_recurred", id=inc.id, kinds=sorted({e.kind for e in self.post_finish}))
                self.recurrence_hint = inc.id
                return True
        self.events.emit("incident_verified_quiet", id=inc.id, seconds=int(self.cfg.verify_window_s))
        return False

    async def _evolve(self, inc: Incident, idir: Path) -> None:
        edir = idir / "evolve"
        edir.mkdir(parents=True, exist_ok=True)
        before = await asyncio.to_thread(snapshot, self.cfg)
        self.events.emit("evolution_started", id=inc.id)
        try:
            toolbox = ToolBox(self.cfg, self.guard, self.audit, self.events, f"{inc.id}-evolve", edir)
            res = await run_session(
                self.cfg, self.llm, toolbox, system_prompt(self.cfg), evolve_prompt(self.cfg, inc.id, idir),
                max_turns=self.cfg.evolve_max_turns, timeout_s=self.cfg.evolve_timeout_s,
                transcript_path=edir / "transcript.jsonl", events=self.events, kind="evolve",
            )
            changes = await asyncio.to_thread(validate_changes, self.cfg, before)
            self._write_meta(edir / "evolve.json", {
                "incident": inc.id, "finished_at": utcnow_iso(), "resolution": res.resolution, "summary": res.summary,
                "turns": res.turns, "tool_calls": res.tool_calls, "denied_calls": res.denied_calls,
                "total_cost_usd": res.cost_usd, "changes": changes, "duration_s": round(res.duration_s, 1),
                "usage": {"prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens, "cached_tokens": res.cached_tokens},
            })
            self.events.emit("evolution_done", id=inc.id, resolution=res.resolution, turns=res.turns, changes=changes, summary=res.summary[:300])
        except Exception as exc:
            log.exception("evolve failed")
            self.events.emit("evolution_error", id=inc.id, error=f"{exc.__class__.__name__}: {exc}")
