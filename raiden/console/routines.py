"""Scheduled routines: the regular work of a DBA, on a cron.

`check` routines are deterministic (SQL and metrics, no tokens); `agent` routines are short AI sessions with read-only
tools that end in findings. Findings become issue/suggestion chips on the server; CRIT findings of routines set to
`incident` open an incident. The `dream` routine is the fleet's nightly learning pass (dreams.py).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlalchemy import select

from . import assessment, fmt, learning
from .agent.loop import AgentLoop
from .agent.prompts import PLATFORM_NOTES, SYSTEM_ROUTINE
from .agent.tools import ToolContext, ToolOutcome, server_brief, tools_for
from .catalog import BY_ID, health, split_key
from .cron import Cron
from .db import utcnow
from .models import AgentSession, Routine, RoutineRun, Server, Suggestion
from .monitors import in_scope
from .services import create_incident, open_incident_for, open_incidents_on, upsert_suggestion

log = logging.getLogger("raiden.console.routines")

TEMPLATES: list[dict[str, Any]] = [
    dict(key="health-patrol", name="Health patrol", kind="check", cron="*/15 * * * *", checks=["metrics", "id:pending-restart", "prefix:slot-", "id:xid-age"],
         on_finding="incident", description="Every key metric against its thresholds, settings waiting for a restart, abandoned replication slots and "
         "transaction ID age. Deterministic, no tokens."),
    dict(key="security-posture", name="Security posture", kind="check", cron="0 6 * * *", checks=["area:security"], on_finding="suggest",
         description="Password hashing, TLS, superusers, schema public privileges, audit logging and risky extensions."),
    dict(key="config-drift", name="Configuration drift", kind="check", cron="0 * * * *", checks=["id:pending-restart", "prefix:durability-", "id:autovacuum-off"],
         on_finding="incident", description="Settings changed in the files that wait for the next restart (a time bomb), and durability switched off."),
    dict(key="wraparound-watch", name="Wraparound watch", kind="check", cron="5 * * * *", checks=["id:xid-age", "prefix:slot-", "metrics:maximum_used_transactionIDs,prepared_xact_oldest_seconds"],
         on_finding="suggest", description="Transaction ID age, prepared transactions and replication slots that hold the xmin horizon."),
    dict(key="backup-readiness", name="Backup & recovery readiness", kind="check", cron="30 5 * * *",
         checks=["id:archiving", "id:checksums", "id:no-standby", "metrics:archive_failed,archive_lag_seconds"], on_finding="suggest",
         description="WAL archiving, data checksums, a standby to fail over to, and the age of the last archived WAL segment."),
    dict(key="config-review", name="Configuration review", kind="check", cron="0 4 * * 0", checks=["assessment"], on_finding="report",
         description="The full assessment (configuration, performance, reliability, security, observability, maintenance) with sizing advice."),
    dict(key="daily-report", name="Daily health report", kind="agent", cron="0 7 * * *", on_finding="report",
         prompt="Summarize the last 24 hours of this server for its owner: anything unusual in the key metrics (use metric_history over 1440 minutes for "
                "cpu_percent, connections_percent, tps, storage_percent where available), recent incidents, and the top three things to look at this week.",
         description="An AI summary of the last 24 hours per server: anomalies, incidents, capacity trends and the top three recommendations."),
    dict(key="index-advisor", name="Index advisor", kind="agent", cron="0 8 * * 1", on_finding="suggest",
         prompt="Find index problems: statements with a high mean time and sequential scans on large tables (top_queries by mean_time, table_health), "
                "unused and duplicate indexes, invalid indexes. Each finding names the table and the exact CREATE/DROP INDEX CONCURRENTLY statement.",
         description="Weekly AI review of pg_stat_statements and the catalog: missing, unused, duplicate and invalid indexes."),
    dict(key="vacuum-review", name="Vacuum & bloat review", kind="agent", cron="0 7 * * 1", on_finding="suggest",
         prompt="Review vacuum health: the tables with the most dead rows (table_health), their last autovacuum, transaction ID age (wraparound_status) and "
                "the autovacuum settings (get_settings pattern autovacuum). Recommend per-table autovacuum settings where the defaults do not keep up.",
         description="Weekly AI review of dead rows, autovacuum settings and freezing, with per-table recommendations."),
    dict(key="dream", name="Dream", kind="dream", cron="0 3 * * *", on_finding="report",
         description="Once a night rAIden turns the team's decisions, notes and chats into instructions and playbooks, and proposes not to ask again "
                     "for what people always approve. It reads only what is new; with nothing new it makes no model call."),
]
BY_KEY = {t["key"]: t for t in TEMPLATES}
DEFAULT_ON = {"health-patrol", "security-posture", "config-drift", "wraparound-watch", "backup-readiness", "daily-report", "dream"}


def seed_builtin(s: Any) -> int:
    have = {r.key for r in s.execute(select(Routine)).scalars()}
    added = 0
    now = utcnow()
    for t in TEMPLATES:
        if t["key"] in have:
            continue
        s.add(Routine(key=t["key"], name=t["name"], description=t["description"], kind=t["kind"], checks=t.get("checks", []), prompt=t.get("prompt", ""),
                      cron=t["cron"], on_finding=t["on_finding"], enabled=t["key"] in DEFAULT_ON, builtin=True, next_run_at=Cron(t["cron"]).next_after(now)))
        added += 1
    return added


def metric_findings(latest: dict[str, float], only: list[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for key, v in latest.items():
        base, dim = split_key(key)
        if dim is not None or base not in BY_ID or (only and base not in only):
            continue
        h = health(base, v)
        if h == "ok":
            continue
        from . import fmt

        m = BY_ID[base]
        out.append(assessment.finding(f"metric-{base}", m.category if m.category in assessment.AREAS else "performance", "issue" if h == "crit" else "suggestion",
                                      f"{m.name} is {fmt.value(base, v)}", m.description, "Look at the trend on the server page; rAIden investigates CRIT values.",
                                      fmt.value(base, v)))
    return out


def select_findings(sel: list[str], facts: dict[str, Any], latest: dict[str, float]) -> list[dict[str, Any]]:
    checks = assessment.run_checks(facts) if any(not x.startswith("metrics") for x in sel) else []
    out: list[dict[str, Any]] = []
    for x in sel:
        if x == "metrics":
            out += metric_findings(latest)
        elif x.startswith("metrics:"):
            out += metric_findings(latest, x.split(":", 1)[1].split(","))
        elif x.startswith("area:"):
            out += [f for f in checks if f["area"] == x[5:]]
        elif x.startswith("id:"):
            out += [f for f in checks if f["id"] == x[3:]]
        elif x.startswith("prefix:"):
            out += [f for f in checks if f["id"].startswith(x[7:])]
        elif x == "assessment":
            out += checks
    seen, uniq = set(), []
    for f in out:
        if f["id"] not in seen:
            seen.add(f["id"])
            uniq.append(f)
    return uniq


class RoutineRunner:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.running: set[int] = set()
        self.agent_sem = asyncio.Semaphore(1)

    async def tick(self) -> None:
        now = utcnow()
        with self.rt.db.session() as s:
            due = [r.id for r in s.execute(select(Routine).where(Routine.enabled.is_(True), Routine.next_run_at <= now)).scalars()]
            for r in s.execute(select(Routine).where(Routine.enabled.is_(True), Routine.next_run_at.is_(None))).scalars():
                r.next_run_at = Cron(r.cron).next_after(now, r.timezone)
        for rid in due:
            if rid not in self.running:
                self.running.add(rid)
                asyncio.create_task(self._guarded(rid, "schedule"))

    async def run_now(self, rid: int, user: str) -> int:
        with self.rt.db.session() as s:
            run = RoutineRun(routine_id=rid, triggered_by=user, status="running")
            s.add(run)
            s.flush()
            run_id = run.id
        self.running.add(rid)
        asyncio.create_task(self._guarded(rid, user, run_id))
        return run_id

    async def _guarded(self, rid: int, by: str, run_id: int | None = None) -> None:
        try:
            await self.run(rid, by, run_id)
        except Exception:  # noqa: BLE001
            log.exception("routine %s failed", rid)
        finally:
            self.running.discard(rid)

    async def run(self, rid: int, by: str, run_id: int | None = None) -> None:
        with self.rt.db.session() as s:
            r = s.get(Routine, rid)
            if r is None:
                return
            r.next_run_at = Cron(r.cron).next_after(utcnow(), r.timezone)
            if run_id is None:
                run = RoutineRun(routine_id=rid, triggered_by=by, status="running")
                s.add(run)
                s.flush()
                run_id = run.id
            servers = [srv.id for srv in s.execute(select(Server).where(Server.enabled.is_(True)).order_by(Server.id)).scalars()
                       if _scoped(r, srv)]
            kind, key, sel, prompt, name, on_finding = r.kind, r.key or f"routine-{r.id}", list(r.checks or []), r.prompt, r.name, r.on_finding
        if kind == "dream":
            await self._dream(rid, run_id, by)
            return
        self.rt.bus.publish("routine", id=rid, run_id=run_id, status="running")
        results, tokens_in, tokens_out = [], 0, 0
        for sid in servers:
            try:
                if kind == "check":
                    res = await self._check(sid, sel, key)
                else:
                    async with self.agent_sem:
                        res, ti, to = await self._agent(sid, name, prompt, run_id)
                    tokens_in += ti
                    tokens_out += to
            except Exception as exc:  # noqa: BLE001
                res = {"server_id": sid, "status": "error", "summary": f"{exc.__class__.__name__}: {exc}", "findings": []}
            results.append(res)
            self._apply(sid, key, name, on_finding, res)
        worst = "crit" if any(r["status"] == "crit" for r in results) else "warn" if any(r["status"] == "warn" for r in results) else \
            "error" if results and all(r["status"] == "error" for r in results) else "ok"
        n_find = sum(len(r["findings"]) for r in results)
        summary = f"{fmt.plural(len(results), 'server')}, " + (f"{fmt.plural(n_find, 'finding')}; worst: {worst.upper()}" if worst != "ok" else "no findings")
        report = "\n\n".join(f"### {r['server_id']}\n{r.get('summary', '')}" for r in results if r.get("summary"))
        with self.rt.db.session() as s:
            run = s.get(RoutineRun, run_id)
            run.finished_at, run.status, run.summary, run.results, run.report = utcnow(), worst, summary, results, report
            run.tokens_in, run.tokens_out = tokens_in, tokens_out
            r = s.get(Routine, rid)
            if r:
                r.last_run_at, r.last_status = utcnow(), worst
        self.rt.bus.publish("routine", id=rid, run_id=run_id, status=worst)

    async def _dream(self, rid: int, run_id: int, by: str) -> None:
        """The fleet's dream, not a server's: one run, its summary and journal as the run's report."""
        from .dreams import DreamBusy

        self.rt.bus.publish("routine", id=rid, run_id=run_id, status="running")
        try:
            res = await self.rt.dreamer.run(run_id, by)
            status = "ok" if res["status"] == "done" else "error"
            summary, report, ti, to = res["summary"], res["report"], res["tokens_in"], res["tokens_out"]
        except DreamBusy as exc:
            status, summary, report, ti, to = "ok", f"{exc}: this run left it alone", "", 0, 0
        except Exception as exc:  # noqa: BLE001
            log.exception("dream failed")
            status, summary, report, ti, to = "error", f"{exc.__class__.__name__}: {exc}", "", 0, 0
        with self.rt.db.session() as s:
            run = s.get(RoutineRun, run_id)
            run.finished_at, run.status, run.summary, run.report, run.tokens_in, run.tokens_out = utcnow(), status, summary, report, ti, to
            r = s.get(Routine, rid)
            if r:
                r.last_run_at, r.last_status = utcnow(), status
        self.rt.bus.publish("routine", id=rid, run_id=run_id, status=status)

    async def _check(self, sid: str, sel: list[str], key: str) -> dict[str, Any]:
        latest = self.rt.store.latest_values(sid)
        facts = await self.rt.server_facts(sid) if any(not x.startswith("metrics") for x in sel) else {}
        if "assessment" in sel:
            aid = await self.rt.assess(sid, trigger="routine", user="schedule", facts=facts)
            findings = select_findings(sel, facts, latest)
            return {"server_id": sid, "status": _status(findings), "findings": findings, "assessment_id": aid,
                    "summary": f"{fmt.plural(sum(f['severity'] == 'issue' for f in findings), 'issue')}, "
                               f"{fmt.plural(sum(f['severity'] == 'suggestion' for f in findings), 'suggestion')}"}
        findings = select_findings(sel, facts, latest)
        return {"server_id": sid, "status": _status(findings), "findings": findings,
                "summary": "; ".join(f["title"] for f in findings[:4]) if findings else "nothing found"}

    async def _agent(self, sid: str, name: str, task: str, run_id: int) -> tuple[dict[str, Any], int, int]:
        with self.rt.db.session() as s:
            srv = s.get(Server, sid)
            system = SYSTEM_ROUTINE.format(name=name, server=server_brief(s, sid), platform_note=PLATFORM_NOTES.get(srv.platform, ""), task=task,
                                           knowledge=learning.prompt_block(s, sid, srv.environment))
            sess = AgentSession(id=uuid.uuid4().hex, kind="routine", routine_run_id=run_id, server_id=sid,
                                messages=[{"role": "system", "content": system}, {"role": "user", "content": f"Run the routine now on {srv.name}. {task}"}])
            s.add(sess)
            sess_id = sess.id
        result: dict[str, Any] = {}

        async def special(n: str, a: dict[str, Any], tc: str) -> ToolOutcome:
            if n == "submit_findings":
                result.update(a)
                return ToolOutcome(text="recorded", done={"tool": n, "args": a})
            return ToolOutcome(text=f"ERROR: {n} is not available in a routine", ok=False)

        loop = AgentLoop(self.rt, sess_id, "routines", ToolContext(self.rt, sid, session_id=sess_id, on_special=special))
        res = await loop.run(tools_for("routine"), 12)
        with self.rt.db.session() as s:
            sess = s.get(AgentSession, sess_id)
            ti, to = sess.tokens_in, sess.tokens_out
            sess.status = "done"
        if res.status != "done":
            return {"server_id": sid, "status": "error", "summary": res.error or "the routine did not finish", "findings": []}, ti, to
        findings = [assessment.finding(f"ai-{i}", f.get("category", "performance") if f.get("category") in assessment.AREAS else "performance",
                                       f.get("kind", "suggestion"), f.get("title", ""), f.get("detail", ""), f.get("detail", ""))
                    for i, f in enumerate(result.get("findings") or [])]
        status = {"ok": "ok", "warn": "warn", "crit": "crit"}.get(result.get("status", "ok"), "ok")
        return {"server_id": sid, "status": status, "summary": result.get("summary", ""), "findings": findings}, ti, to

    def _apply(self, sid: str, key: str, name: str, on_finding: str, res: dict[str, Any]) -> None:
        if res["status"] == "error":
            return
        prefix = f"routine:{key}:"
        with self.rt.db.session() as s:
            current = set()
            for f in res["findings"]:
                fp = prefix + f["id"]
                current.add(fp)
                if on_finding in ("suggest", "incident"):
                    upsert_suggestion(s, sid, kind=f["severity"] if f["severity"] in ("issue", "suggestion") else "suggestion", category=f["area"],
                                      title=f["title"], detail=f"{f['detail']}\n\n**Recommendation:** {f['recommendation']}", source=f"routine:{key}",
                                      fingerprint=fp, data={"finding": f, "routine": name})
                if (on_finding == "incident" and f["severity"] == "issue" and not f["id"].startswith("metric-")  # metrics belong to the monitors
                        and not open_incident_for(s, f"routine:{key}:{sid}:{f['id']}")
                        and not any(i.category == _cat(f) for i in open_incidents_on(s, sid))):
                    inc = create_incident(s, server_id=sid, title=f["title"], severity="sev3", category=_cat(f), source="routine", source_ref=name,
                                          fingerprint=f"routine:{key}:{sid}:{f['id']}", summary=f"{f['detail']} {f['recommendation']}",
                                          evidence={"routine": key, "finding": f["id"], "evidence": f.get("evidence", "")},
                                          scenario=_scenario(f["id"]) if self.rt.fleet is not None else "")
                    self.rt.bus.publish("incident", id=inc.id)
            for sug in s.execute(select(Suggestion).where(Suggestion.server_id == sid, Suggestion.fingerprint.like(prefix + "%"), Suggestion.status == "open")).scalars():
                if sug.fingerprint not in current:
                    sug.status = "resolved"


def _status(findings: list[dict[str, Any]]) -> str:
    return "crit" if any(f["severity"] == "issue" for f in findings) else "warn" if findings else "ok"


def _scenario(finding_id: str) -> str:
    """Demo only: which playbook of the scripted model fits a finding the patrol turned into an incident."""
    if finding_id == "pending-restart":
        return "pending_restart"
    if finding_id.startswith("slot-"):
        return "wal_slot"
    if finding_id == "xid-age":
        return "wraparound"
    return ""


def _cat(f: dict[str, Any]) -> str:
    fid = f["id"]
    for k, c in (("slot", "replication"), ("xid", "wraparound"), ("pending", "configuration"), ("invalid", "performance"), ("durability", "configuration")):
        if k in fid:
            return c
    return f["area"]


def _scoped(r: Routine, srv: Server) -> bool:
    class _M:  # reuse the monitor scope rules
        scope = r.scope or {}
    return in_scope(_M(), srv)  # type: ignore[arg-type]
