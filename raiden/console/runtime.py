"""The console runtime: owns the database, the drivers and every background loop (collection, monitors, the incident
pipeline, routines, maintenance, the demo fleet)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from . import __version__, assessment, fmt
from .ai.router import AIRouter
from .bus import Bus
from .catalog import BY_ID, PLATFORMS, health, split_key
from .db import Database, StoreLock, iso, utcnow
from .drivers.azure_monitor import AzureMonitorSource
from .drivers.base import CollectResult, DriverError
from .drivers.postgres import PostgresDriver
from .models import Assessment, Incident, Monitor, MonitorState, Routine, Server, Suggestion
from .monitors import evaluate_server, title_for
from .pipeline import Pipeline
from .routines import RoutineRunner
from .security import Keyring, LoginThrottle
from .services import add_event, create_incident, get_setting, open_incident_for, open_incidents_on, put_setting, resolve_suggestion, upsert_suggestion
from .settings import ConsoleSettings

log = logging.getLogger("raiden.console")

SEV_RANK = {"sev1": 1, "sev2": 2, "sev3": 3, "sev4": 4}



def _version(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


class Runtime:
    def __init__(self, settings: ConsoleSettings) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(settings.database_url)
        self.db.create_all()
        self.store_lock = StoreLock(self.db)
        self.keyring = Keyring.load(settings.secret_key, settings.data_dir)
        self.login_throttle = LoginThrottle()
        from .timeseries import MetricStore

        self.store = MetricStore(self.db)
        self.bus = Bus()
        self.fleet = None
        self.demo_provider = None
        if settings.demo:
            from .demo.fleet import DemoFleet
            from .demo.model import DemoProvider

            self.fleet = DemoFleet(speed=settings.demo_speed)
            self.demo_provider = DemoProvider(settings.demo_speed)
        self.router = AIRouter(self.db, self.keyring, demo_provider=self.demo_provider.as_model if self.demo_provider else None)
        self.db.on_commit = lambda kind, data: self.notify(kind, **data)
        self.pipeline = Pipeline(self)
        self.routines = RoutineRunner(self)
        from .chat import ChatService

        self.chat = ChatService(self)
        self._drivers: dict[str, tuple[str, Any]] = {}
        self._azure: dict[str, tuple[AzureMonitorSource, float, dict[str, float]]] = {}
        self._collectors: dict[str, tuple[str, asyncio.Task[None]]] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self.started_at = time.time()

    # ================================================================================================ lifecycle
    async def bootstrap(self) -> None:
        from .seed import seed_defaults

        self.store_lock.acquire()  # before anything is written: one console per state store
        try:
            with self.db.session() as s:
                # an older console on a state store a newer one has written could misread it: refuse instead
                seen = str(get_setting(s, "schema").get("version") or "0")
                if _version(seen) > _version(__version__):
                    raise RuntimeError(f"this state store was last used by rAIden {seen}, this is {__version__}: run {seen} or newer, "
                                       "or restore the backup taken before the upgrade")
                put_setting(s, "schema", {"version": __version__, "started_at": iso(utcnow())})
                seed_defaults(s, demo=self.settings.demo)
            if self.settings.demo:
                from .demo.seed import seed_demo

                await seed_demo(self)
        except BaseException:
            self.store_lock.release()
            raise

    async def start(self) -> None:
        self.bus.attach(asyncio.get_running_loop())
        with self.db.session() as s:  # incidents interrupted by a restart pick up where they were
            for inc in s.execute(select(Incident).where(Incident.state == "running")).scalars():
                inc.state = "queued"
        loops = [self._supervise(), self._every(1.5, self.pipeline.tick), self._every(20, self.routines.tick), self._every(600, self._maintenance),
                 self._every(30, self._hold_store)]
        if self.fleet is not None:
            loops.append(self._every(5, self._demo_tick))
        self._tasks = [asyncio.create_task(c) for c in loops]

    async def stop(self) -> None:
        for t in self._tasks + [t for _, t in self._collectors.values()]:
            t.cancel()
        for _, d in self._drivers.values():
            try:
                await d.close()
            except Exception:  # noqa: BLE001
                pass
        self.store_lock.release()

    async def _hold_store(self) -> None:
        if not await asyncio.to_thread(self.store_lock.check):
            # the store restarted and another console took it before this one could: two would act twice, so this one goes
            log.critical("another rAIden Console took over this state store while it was unreachable: stopping this one")
            os.kill(os.getpid(), signal.SIGTERM)

    async def _every(self, seconds: float, fn: Any) -> None:
        while True:
            try:
                res = fn()
                if asyncio.iscoroutine(res):
                    await res
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(seconds)

    async def _demo_tick(self) -> None:
        self.fleet.tick(time.time())

    async def _maintenance(self) -> None:
        now = int(time.time())
        await asyncio.to_thread(self.store.rollup, now)
        with self.db.session() as s:
            g = get_setting(s, "general")
        await asyncio.to_thread(self.store.prune, now, float(g.get("retention_raw_days", 3)), float(g.get("retention_rollup_days", 93)))

    # ================================================================================================ drivers
    def _driver_key(self, srv: Server) -> str:
        raw = json.dumps([srv.host, srv.port, srv.dbname, srv.username, srv.password_enc, srv.sslmode, srv.databases, (srv.info or {}).get("simulated")], default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def driver(self, server_id: str) -> Any:
        with self.db.session() as s:
            srv = s.get(Server, server_id)
            if srv is None:
                raise DriverError(f"no server {server_id}")
            key = self._driver_key(srv)
            hit = self._drivers.get(server_id)
            if hit and hit[0] == key:
                return hit[1]
            if (sim := (srv.info or {}).get("simulated")):
                if self.fleet is None:
                    raise DriverError(f"{srv.name} is a simulated demo server; start the console with --demo")
                from .demo.driver import DemoDriver

                drv: Any = DemoDriver(server_id, self.fleet, sim if isinstance(sim, str) else None)
            else:
                drv = PostgresDriver(server_id, self.conninfo(srv), srv.databases or None)
        if hit:
            asyncio.ensure_future(hit[1].close())
        self._drivers[server_id] = (key, drv)
        return drv

    def conninfo(self, srv: Server, password: str | None = None) -> dict[str, Any]:
        return {"host": srv.host, "port": srv.port, "dbname": srv.dbname, "user": srv.username, "sslmode": srv.sslmode,
                "password": password if password is not None else self.keyring.decrypt(srv.password_enc)}

    # ================================================================================================ collection
    async def _supervise(self) -> None:
        while True:
            try:
                with self.db.session() as s:
                    want = {srv.id: f"{srv.collect_interval_s}:{self._driver_key(srv)}" for srv in s.execute(select(Server).where(Server.enabled.is_(True))).scalars()}
                for sid, (sig, task) in list(self._collectors.items()):
                    if want.get(sid) != sig or task.done():
                        task.cancel()
                        del self._collectors[sid]
                for sid, sig in want.items():
                    if sid not in self._collectors:
                        self._collectors[sid] = (sig, asyncio.create_task(self._collect_loop(sid)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("collector supervisor failed")
            await asyncio.sleep(5)

    async def _collect_loop(self, sid: str) -> None:
        self.store.warm(sid, int(time.time()))
        while True:
            with self.db.session() as s:
                srv = s.get(Server, sid)
                if srv is None or not srv.enabled:
                    return
                interval = max(5, srv.collect_interval_s)
                azure_id = srv.azure_resource_id if not (srv.info or {}).get("simulated") else None
                host_metrics = dict((srv.info or {}).get("host_metrics") or {}) if time.time() - float((srv.info or {}).get("host_metrics_at") or 0) < 180 else {}
            started = time.monotonic()
            try:
                res: CollectResult = await self.driver(sid).collect()
            except DriverError as exc:
                res = CollectResult(ok=False, metrics={"is_db_alive": 0.0}, error=str(exc))
            for k, v in host_metrics.items():  # CPU, memory, disk from the rAIden host agent on a self-managed host
                res.metrics.setdefault(k, v)
            if azure_id:
                res.metrics.update(await self._azure_metrics(sid, azure_id))
            try:
                await asyncio.to_thread(self._after_collect, sid, res, interval)
            except Exception:  # noqa: BLE001
                log.exception("post-collection for %s failed", sid)
            self.bus.publish("metrics", server=sid)
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - started)))

    async def _azure_metrics(self, sid: str, resource_id: str) -> dict[str, float]:
        src, at, last = self._azure.get(sid, (AzureMonitorSource(resource_id), 0.0, {}))
        if time.time() - at < 55:
            return last
        try:
            last = await src.fetch()
        except Exception as exc:  # noqa: BLE001 - Azure Monitor is an extra source; SQL metrics still flow
            log.warning("Azure Monitor for %s: %s", sid, exc)
        self._azure[sid] = (src, time.time(), last)
        return last

    def _after_collect(self, sid: str, res: CollectResult, interval: int) -> None:
        now = int(time.time())
        self.store.append(sid, now, res.metrics)
        opened: list[str] = []
        recurrences: list[tuple[str, str]] = []
        with self.db.session() as s:
            srv = s.get(Server, sid)
            if srv is None:
                return
            if res.ok:
                srv.last_seen_at, srv.last_error = utcnow(), ""
                info = dict(srv.info or {})
                info.update(res.info)
                srv.info = info
            else:
                srv.last_error = res.error
            monitors = list(s.execute(select(Monitor).where(Monitor.enabled.is_(True))).scalars())
            by_id = {m.id: m for m in monitors}
            transitions = evaluate_server(s, srv, monitors, lambda w: self.store.window(sid, w, now), now, interval)
            for t in transitions:
                mon = by_id[t.monitor_id]
                fp = f"mon:{mon.key or mon.id}:{sid}"
                if t.new == "firing":
                    if mon.action == "suggest":
                        upsert_suggestion(s, sid, kind="issue" if mon.severity in ("sev1", "sev2") else "suggestion", category=mon.category,
                                          title=title_for(mon, srv.name, t.value), detail=(mon.description + ("\n\n" + mon.runbook if mon.runbook else "")).strip(),
                                          source=f"monitor:{mon.key or mon.id}", fingerprint=fp, data={"metric": mon.metric, "value": t.value})
                        continue
                    existing = open_incident_for(s, fp)
                    if existing is not None:
                        if existing.stage == "verify":
                            recurrences.append((existing.id, f"{mon.name}: {fmt.value(mon.metric, t.value)}"))
                        else:
                            add_event(s, existing, "signal", f"{mon.name} fired again: {fmt.value(mon.metric, t.value)}", actor="system")
                        continue
                    recent = [i for i in open_incidents_on(s, sid) if utcnow() - i.detected_at < timedelta(minutes=15)]
                    if recent:
                        inc = recent[0]
                        add_event(s, inc, "signal", f"Correlated signal: {title_for(mon, srv.name, t.value)}", mon.description, actor="system",
                                  data={"monitor": mon.name, "metric": mon.metric, "value": t.value})
                        ev = dict(inc.evidence or {})
                        ev.setdefault("correlated", []).append({"monitor": mon.name, "metric": mon.metric, "value": t.value})
                        inc.evidence = ev
                        continue
                    inc = create_incident(
                        s, server_id=sid, title=title_for(mon, srv.name, t.value), severity=mon.severity, category=mon.category, source="monitor",
                        source_ref=mon.name, fingerprint=fp, summary=mon.description,
                        evidence={"monitor": mon.name, "monitor_key": mon.key, "metric": mon.metric, "value": t.value, "threshold": mon.threshold,
                                  "operator": mon.operator, "window_minutes": mon.window_minutes, "aggregation": mon.aggregation, "readable": fmt.value(mon.metric, t.value)},
                        scenario=self.demo_scenario(sid),
                    )
                    if mon.action == "notify":
                        inc.state, inc.assignee, inc.waiting_reason = "escalated", "on-call", "This monitor notifies people only"
                    st = s.get(MonitorState, (mon.id, sid))
                    if st:
                        st.incident_id = inc.id
                    opened.append(inc.id)
                elif t.old == "firing" and t.new == "ok":
                    if mon.action == "suggest":
                        resolve_suggestion(s, sid, fp)
                        continue
                    existing = open_incident_for(s, fp)
                    if existing is not None:
                        add_event(s, existing, "signal", f"{mon.name} back to normal: {fmt.value(mon.metric, t.value)}", actor="system",
                                  data={"metric": mon.metric, "value": t.value, "recovered": True})
            self._status(s, srv, res)
        for iid in opened:
            self.bus.publish("incident", id=iid)
            self.notify("incident_opened", incident_id=iid)
        for iid, why in recurrences:
            self.pipeline.recurrence(iid, why)

    def _status(self, s: Any, srv: Server, res: CollectResult) -> None:
        old = srv.status
        if not srv.enabled:
            srv.status, srv.status_reason = "paused", "collection paused"
            return
        if not res.ok or res.metrics.get("is_db_alive", 1) < 1:
            srv.status, srv.status_reason = "unreachable", res.error or "not accepting connections"
        else:
            incs = open_incidents_on(s, srv.id)
            firing = [(st, s.get(Monitor, st.monitor_id)) for st in s.execute(select(MonitorState).where(MonitorState.server_id == srv.id, MonitorState.state == "firing")).scalars()]
            crit_inc = sorted((i for i in incs if i.severity in ("sev1", "sev2")), key=lambda i: SEV_RANK.get(i.severity, 9))
            crit_mon = [m for _, m in firing if m and m.severity in ("sev1", "sev2") and m.action != "suggest"]
            warn_metrics = [(k, v) for k, v in res.metrics.items() if split_key(k)[1] is None and k in BY_ID and BY_ID[k].key and health(k, v) != "ok"]
            if crit_inc:
                srv.status, srv.status_reason = "critical", f"{crit_inc[0].id}: {crit_inc[0].title}"
            elif crit_mon:
                srv.status, srv.status_reason = "critical", crit_mon[0].name
            elif incs:
                srv.status, srv.status_reason = "warning", f"{incs[0].id}: {incs[0].title}"
            elif firing or warn_metrics:
                if firing and firing[0][1]:
                    srv.status, srv.status_reason = "warning", firing[0][1].name
                else:
                    k, v = warn_metrics[0]
                    srv.status, srv.status_reason = "warning", f"{BY_ID[k].name} {fmt.value(k, v)}"
            else:
                srv.status, srv.status_reason = "healthy", ""
        if old != srv.status:
            self.bus.publish("estate", server=srv.id, status=srv.status)

    # ================================================================================================ helpers for tools
    async def refresh(self, sid: str, max_age: float = 5.0) -> None:
        """Collect now if the newest sample is older than max_age: the AI reads what is true after its own action."""
        snap = self.store.latest(sid)
        if snap and time.time() - snap[0] <= max_age:
            return
        lock = self._refresh_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            snap = self.store.latest(sid)
            if snap and time.time() - snap[0] <= max_age:
                return
            try:
                res = await self.driver(sid).collect()
            except DriverError as exc:
                res = CollectResult(ok=False, metrics={"is_db_alive": 0.0}, error=str(exc))
            with self.db.session() as s:
                srv = s.get(Server, sid)
                interval = srv.collect_interval_s if srv else 60
            await asyncio.to_thread(self._after_collect, sid, res, interval)

    def demo_scenario(self, sid: str) -> str:
        if self.fleet is None:
            return ""
        with self.db.session() as s:
            srv = s.get(Server, sid)
            if not srv or not (srv.info or {}).get("simulated"):
                return ""
        return self.fleet.scenario_for_incident(sid)

    def current_metrics(self, sid: str | None) -> dict[str, Any]:
        latest = self.store.latest_values(sid) if sid else {}
        keys = [m.id for m in BY_ID.values() if m.key and m.id in latest]
        keys += [k for k, v in latest.items() if split_key(k)[1] is None and k in BY_ID and k not in keys and health(k, v) != "ok"]
        rows = [[k, latest[k], fmt.value(k, latest[k]), health(k, latest[k])] for k in keys]
        return {"columns": ["metric", "value", "readable", "health"], "rows": rows}

    def metric_history(self, sid: str, metric: str, minutes: int, dimension: str | None = None) -> dict[str, Any]:
        key = f"{metric}[{dimension}]" if dimension else metric
        base = split_key(key)[0]
        if base not in BY_ID:
            return {"error": f"unknown metric {metric}; use catalog ids such as cpu_percent, connections_percent, blocked_sessions"}
        end = int(time.time())
        pts = self.store.series(sid, [key], end - minutes * 60, end, max_points=30)[key]
        vals = [p[1] for p in pts]
        m = BY_ID[base]
        return {"metric": key, "unit": m.unit, "minutes": minutes, "min": min(vals) if vals else None, "max": max(vals) if vals else None,
                "avg": round(sum(vals) / len(vals), 3) if vals else None, "last": vals[-1] if vals else None,
                "readable_last": fmt.value(base, vals[-1]) if vals else "no data", "warn": m.warn, "crit": m.crit,
                "points": [[time.strftime("%H:%M", time.gmtime(t)), round(v, 2)] for t, v in pts]}

    def fleet_snapshot(self) -> dict[str, Any]:
        with self.db.session() as s:
            servers = list(s.execute(select(Server).order_by(Server.environment, Server.id)).scalars())
            out = []
            for srv in servers:
                latest = self.store.latest_values(srv.id)
                out.append({"id": srv.id, "name": srv.name, "environment": srv.environment, "platform": srv.platform,
                            "platform_label": PLATFORMS.get(srv.platform, srv.platform), "status": srv.status, "status_reason": srv.status_reason,
                            "metrics": {k: round(latest[k], 2) for k in ("cpu_percent", "connections_percent", "tps", "storage_percent", "blocked_sessions",
                                                                           "physical_replication_delay_in_seconds", "maximum_used_transactionIDs") if k in latest}})
        return {"servers": out}

    def lessons_for(self, s: Any, sid: str | None) -> list[str]:
        lessons = get_setting(s, "lessons")
        return [x["text"] for x in (lessons.get(sid or "", []) + lessons.get("_fleet", []))][-12:]

    def enable_routine(self, s: Any, template: str, sid: str | None) -> str:
        r = s.execute(select(Routine).where(Routine.key == template)).scalar_one_or_none()
        if r is None:
            raise ValueError(f"no routine template {template!r}")
        was = r.enabled
        r.enabled = True
        scope = dict(r.scope or {})
        if not was and sid:
            scope["servers"] = sorted(set(scope.get("servers") or []) | {sid})
            r.scope = scope
        elif scope.get("servers") and sid and sid not in scope["servers"]:
            scope["servers"] = sorted(set(scope["servers"]) | {sid})
            r.scope = scope
        return f"routine {r.name} enabled" + (f" for {sid}" if sid else "")

    async def server_facts(self, sid: str) -> dict[str, Any]:
        facts = await self.driver(sid).facts()
        with self.db.session() as s:
            srv = s.get(Server, sid)
            facts.update(platform=srv.platform, environment=srv.environment, host=(srv.info or {}).get("host") or {})
            azure_id = srv.azure_resource_id if not (srv.info or {}).get("simulated") else None
        facts["metrics"] = self.store.latest_values(sid)
        if azure_id:  # the Azure resource's own security settings, as Fabric's Database Hub assesses them
            try:
                facts["azure"] = await AzureMonitorSource(azure_id).posture()
            except Exception as exc:  # noqa: BLE001 - no Azure access is a gap in the assessment, not a failure of it
                facts["azure_error"] = str(exc)[:300]
        return facts

    async def assess(self, sid: str, trigger: str = "manual", user: str = "", facts: dict[str, Any] | None = None) -> int:
        with self.db.session() as s:
            a = Assessment(server_id=sid, trigger=trigger, triggered_by=user, status="running")
            s.add(a)
            s.flush()
            aid = a.id
            name = s.get(Server, sid).name
        self.bus.publish("assessment", id=aid, server=sid, status="running")
        try:
            facts = facts or await self.server_facts(sid)
            findings = assessment.run_checks(facts)
            tune = assessment.tuning(facts)
            score, per = assessment.scores(findings)
            summary, ti, to = "", 0, 0
            try:
                reply, _ep = await self.router.complete("routines", [{"role": "system", "content": "You are rAIden, a senior PostgreSQL DBA."},
                                                                      {"role": "user", "content": assessment.summary_prompt(name, findings, score)}], None, server_id=sid,
                                                        max_output_tokens=1200)
                summary, ti, to = reply.text.strip(), reply.tokens_in, reply.tokens_out
            except Exception as exc:  # noqa: BLE001 - the checks stand on their own without the AI summary
                summary = assessment.demo_summary(assessment.summary_prompt(name, findings, score)) + f"\n\n*(AI summary unavailable: {str(exc)[:120]})*"
            keep = {k: facts.get(k) for k in ("version", "version_num", "in_recovery", "extensions", "superusers", "databases", "replicas", "host", "platform")}
            with self.db.session() as s:
                a = s.get(Assessment, aid)
                a.status, a.finished_at, a.score, a.scores, a.findings, a.tuning, a.facts, a.summary = "done", utcnow(), score, per, findings, tune, keep, summary
                a.tokens_in, a.tokens_out = ti, to
                current = set()
                for f in findings:
                    if f["severity"] not in ("issue", "suggestion") or f["id"].startswith("tune-"):
                        continue
                    fp = f"assess:{f['id']}"
                    current.add(fp)
                    upsert_suggestion(s, sid, kind=f["severity"], category=f["area"], title=f["title"], detail=f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}",
                                      source="assessment", fingerprint=fp, data={"finding": f, "assessment": aid})
                for sug in s.execute(select(Suggestion).where(Suggestion.server_id == sid, Suggestion.fingerprint.like("assess:%"), Suggestion.status == "open")).scalars():
                    if sug.fingerprint not in current:
                        sug.status = "resolved"
        except Exception as exc:  # noqa: BLE001
            log.exception("assessment of %s failed", sid)
            with self.db.session() as s:
                a = s.get(Assessment, aid)
                a.status, a.finished_at, a.error = "error", utcnow(), f"{exc.__class__.__name__}: {exc}"
        self.bus.publish("assessment", id=aid, server=sid, status="done")
        return aid

    # ================================================================================================ notifications
    def notify(self, kind: str, **data: Any) -> None:
        from .notify import send

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(send(self, kind, data))
        elif self.bus._loop is not None:
            asyncio.run_coroutine_threadsafe(send(self, kind, data), self.bus._loop)
