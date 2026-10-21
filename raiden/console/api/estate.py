"""The estate: overview, servers (onboarding, connection tests, configuration), metrics, live drill-downs and assessments."""

from __future__ import annotations

import asyncio
import re
import statistics
import time
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ..catalog import BY_ID, PLATFORMS, catalog_payload, split_key
from ..db import iso, utcnow
from ..drivers.base import ACTIONS, DriverError
from ..drivers.postgres import PostgresDriver
from ..models import Approval, Assessment, Incident, IncidentEvent, MonitorState, Monitor, Server, Suggestion, TokenUsage, User
from ..security import new_agent_token
from ..services import OPEN_STAGES, audit
from . import serial
from .deps import admin, operator, rt, viewer

router = APIRouter(prefix="/api/v1")

RANGES = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
LIVE = {"overview", "sessions", "locks", "queries", "tables", "replication", "settings", "databases", "wraparound"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class HostInfo(BaseModel):
    ram_gb: float | None = None
    vcpus: int | None = None
    storage: str | None = Field(default=None, pattern="^(ssd|nvme|hdd|network)$")
    workload: str | None = Field(default=None, pattern="^(oltp|olap|mixed)$")


class ServerIn(BaseModel):
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="production", pattern="^(production|staging|development)$")
    platform: str = "self_managed"
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = "postgres"
    username: str = "raiden_monitor"
    password: str | None = None
    sslmode: str = Field(default="prefer", pattern="^(disable|allow|prefer|require|verify-ca|verify-full)$")
    databases: list[str] = []
    azure_resource_id: str | None = None
    tags: dict[str, str] = {}
    owner: str = ""
    description: str = ""
    maintenance_window: str = ""
    collect_interval_s: int = Field(default=60, ge=10, le=3600)
    enabled: bool = True
    host_info: HostInfo | None = None


class ServerPatch(BaseModel):
    name: str | None = None
    environment: str | None = Field(default=None, pattern="^(production|staging|development)$")
    platform: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    dbname: str | None = None
    username: str | None = None
    password: str | None = None
    sslmode: str | None = Field(default=None, pattern="^(disable|allow|prefer|require|verify-ca|verify-full)$")
    databases: list[str] | None = None
    azure_resource_id: str | None = None
    tags: dict[str, str] | None = None
    owner: str | None = None
    description: str | None = None
    maintenance_window: str | None = None
    collect_interval_s: int | None = Field(default=None, ge=10, le=3600)
    enabled: bool | None = None
    host_info: HostInfo | None = None


class ConnTest(BaseModel):
    host: str
    port: int = 5432
    dbname: str = "postgres"
    username: str = "raiden_monitor"
    password: str | None = None
    sslmode: str = "prefer"
    server_id: str | None = None  # re-use the stored password of an existing server


class ActionIn(BaseModel):
    action: str
    args: dict[str, Any] = {}
    reason: str = ""


# ============================================================================================================ helpers
def _latest(runtime: Any, sid: str) -> dict[str, float]:
    return runtime.store.latest_values(sid)


def _spark(runtime: Any, sid: str, key: str, seconds: int = 3600, points: int = 40) -> list[float]:
    end = int(time.time())
    return [p[1] for p in runtime.store.series(sid, [key], end - seconds, end, max_points=points)[key]]


def _chips(s: Any, sid: str) -> dict[str, Any]:
    sugg = list(s.execute(select(Suggestion).where(Suggestion.server_id == sid, Suggestion.status == "open")).scalars())
    firing = [(st, s.get(Monitor, st.monitor_id)) for st in s.execute(select(MonitorState).where(MonitorState.server_id == sid, MonitorState.state == "firing")).scalars()]
    issues = [{"title": m.name, "category": m.category, "source": "monitor", "severity": m.severity} for st, m in firing if m and m.action != "suggest"]
    issues += [{"title": x.title, "category": x.category, "source": x.source, "id": x.id} for x in sugg if x.kind == "issue"]
    suggestions = [{"title": x.title, "category": x.category, "source": x.source, "id": x.id} for x in sugg if x.kind == "suggestion"]
    return {"issues": issues, "suggestions": suggestions}


# ============================================================================================================ catalog & overview
@router.get("/metrics/catalog")
def catalog(_: User = Depends(viewer)) -> dict[str, Any]:
    return {**catalog_payload(), "platforms": [{"id": k, "name": v} for k, v in PLATFORMS.items()], "actions": [{"id": k, **v} for k, v in ACTIONS.items()]}


@router.get("/overview")
def overview(runtime: Any = Depends(rt), _: User = Depends(viewer), range: str = "24h") -> dict[str, Any]:
    span = RANGES.get(range, 86400)
    now = utcnow()
    since = now - timedelta(seconds=span)
    with runtime.db.session() as s:
        servers = list(s.execute(select(Server).order_by(Server.environment, Server.id)).scalars())
        names = {x.id: x.name for x in servers}
        open_incs = list(s.execute(select(Incident).where(Incident.stage.in_(OPEN_STAGES))).scalars())
        window_incs = list(s.execute(select(Incident).where((Incident.resolved_at.is_(None)) | (Incident.resolved_at >= since))
                                     .where(Incident.detected_at <= now)).scalars())
        pending = list(s.execute(select(Approval).where(Approval.status == "pending").order_by(Approval.requested_at)).scalars())
        month = now - timedelta(days=30)
        resolved_30 = list(s.execute(select(Incident).where(Incident.resolved_at >= month)).scalars())
        decided = {a.incident_id for a in s.execute(select(Approval).where(Approval.requested_at >= month, Approval.decided_by.isnot(None),
                                                                            Approval.decided_by != "autopilot")).scalars()}
        events = list(s.execute(select(IncidentEvent).where(IncidentEvent.ts >= since, IncidentEvent.kind.in_(
            ("action", "decision", "diagnosis", "escalated", "hardened", "verification", "detected", "takeover", "report"))).order_by(IncidentEvent.ts.desc()).limit(40)).scalars())
        inc_server = {i.id: i.server_id for i in s.execute(select(Incident.id, Incident.server_id)).all()} if events else {}
        tok = s.execute(select(func.coalesce(func.sum(TokenUsage.tokens_in + TokenUsage.tokens_out), 0), func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
                        .where(TokenUsage.bucket >= since)).one()
        cost_month = s.execute(select(func.coalesce(func.sum(TokenUsage.cost_usd), 0.0)).where(TokenUsage.bucket >= now.replace(day=1, hour=0, minute=0, second=0))).scalar()
        actions = s.execute(select(func.count(IncidentEvent.id)).where(IncidentEvent.ts >= since, IncidentEvent.kind == "action")).scalar() or 0
        inc_titles = {i.id: i for i in open_incs}
        needs = []
        for a in pending:
            inc = inc_titles.get(a.incident_id) if a.incident_id else None
            needs.append({**serial.approval(a), "incident_title": inc.title if inc else None, "severity": inc.severity if inc else None,
                          "server_name": names.get(a.server_id or "", a.server_id)})
        statuses = {k: sum(1 for x in servers if x.status == k) for k in ("healthy", "warning", "critical", "unreachable", "paused", "unknown")}
        mttr = [(i.resolved_at - i.detected_at).total_seconds() for i in resolved_30 if i.resolved_at]
        auto = [i for i in resolved_30 if i.id not in decided and i.assignee == "raiden"]
        attention: list[dict[str, Any]] = []
        crit = [x for x in servers if x.status in ("critical", "unreachable")]
        if crit:
            attention.append({"kind": "critical", "title": f"{len(crit)} server{'s' if len(crit) > 1 else ''} critical", "detail": "; ".join(f"{x.name}: {x.status_reason}" for x in crit[:3]),
                              "servers": [x.id for x in crit], "link": "/estate?status=critical"})
        warn = [x for x in servers if x.status == "warning"]
        if warn:
            attention.append({"kind": "warning", "title": f"{len(warn)} server{'s' if len(warn) > 1 else ''} with warnings", "detail": "; ".join(f"{x.name}: {x.status_reason}" for x in warn[:3]),
                              "servers": [x.id for x in warn], "link": "/estate?status=warning"})
        issues = s.execute(select(Suggestion.server_id, func.count(Suggestion.id)).where(Suggestion.status == "open", Suggestion.kind == "issue").group_by(Suggestion.server_id)).all()
        if issues:
            attention.append({"kind": "issues", "title": f"{sum(n for _, n in issues)} open issues on {len(issues)} server{'s' if len(issues) > 1 else ''}",
                              "detail": "Configuration, security and reliability findings that need a fix.", "servers": [x for x, _ in issues], "link": "/estate?chips=issues"})
        sug = s.execute(select(func.count(Suggestion.id)).where(Suggestion.status == "open", Suggestion.kind == "suggestion")).scalar() or 0
        if sug:
            attention.append({"kind": "suggestions", "title": f"{sug} suggestions", "detail": "Tuning, observability and maintenance improvements.", "servers": [],
                              "link": "/estate?chips=suggestions"})
        timeline_incs = [{"id": i.id, "server_id": i.server_id, "title": i.title, "severity": i.severity, "stage": i.stage, "state": i.state,
                          "start": iso(i.detected_at), "end": iso(i.resolved_at)} for i in window_incs if i.server_id]
        decisions = [{"ts": iso(e.ts), "incident_id": e.incident_id, "server_id": inc_server.get(e.incident_id), "actor": e.actor, "kind": e.kind}
                     for e in events if e.kind in ("decision", "action")]
    fleet_bars: dict[str, list[dict[str, Any]]] = {"cpu_percent": [], "memory_percent": [], "storage_percent": [], "connections_percent": []}
    for x in servers:
        latest = _latest(runtime, x.id)
        for k in fleet_bars:
            if k in latest:
                fleet_bars[k].append({"server_id": x.id, "name": x.name, "value": round(latest[k], 1), "color_slot": x.color_slot})
    return {
        "range": range, "start": iso(since), "end": iso(now),
        "kpis": {"servers": len(servers), **statuses, "open_incidents": len(open_incs), "waiting_for_people": len(pending),
                 "escalated": sum(1 for i in open_incs if i.state == "escalated"), "resolved_30d": len(resolved_30),
                 "autopilot_resolved_30d": len(auto), "mttr_median_s": statistics.median(mttr) if mttr else None, "actions": actions,
                 "tokens": int(tok[0]), "cost": round(float(tok[1]), 2), "cost_month": round(float(cost_month or 0), 2)},
        "needs_you": needs, "attention": attention,
        "timeline": {"servers": [{"id": x.id, "name": x.name, "environment": x.environment, "status": x.status, "color_slot": x.color_slot} for x in servers],
                     "incidents": timeline_incs, "decisions": decisions},
        "fleet": fleet_bars,
        "activity": [{"ts": iso(e.ts), "incident_id": e.incident_id, "server_id": inc_server.get(e.incident_id), "kind": e.kind, "title": e.title, "actor": e.actor}
                     for e in events[:18]],
    }


@router.get("/estate")
def estate(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    out = []
    with runtime.db.session() as s:
        servers = list(s.execute(select(Server).order_by(Server.environment, Server.id)).scalars())
        incs = list(s.execute(select(Incident).where(Incident.stage.in_(OPEN_STAGES))).scalars())
        policy = runtime.pipeline.policy(s)
        for x in servers:
            latest = _latest(runtime, x.id)
            mine = [i for i in incs if i.server_id == x.id]
            env_policy = policy["environments"].get(x.environment, {})
            human = [st for st in env_policy if (x.autonomy or {}).get(st, env_policy[st]) == "human"]
            last = s.execute(select(Assessment.score, Assessment.finished_at).where(Assessment.server_id == x.id, Assessment.status == "done")
                             .order_by(Assessment.id.desc()).limit(1)).first()
            out.append({**serial.server(x), "metrics": {k: round(v, 3) for k, v in latest.items() if split_key(k)[1] is None},
                        "assessment": {"score": last.score, "finished_at": iso(last.finished_at)} if last else None,
                        "spark": {k: _spark(runtime, x.id, k) for k in ("cpu_percent", "connections_percent", "tps") if k in latest},
                        "chips": _chips(s, x.id), "incidents": {"open": len(mine), "worst": min((i.severity for i in mine), default=None),
                                                                 "ids": [i.id for i in mine][:3]},
                        "supervised_stages": human})
    return out


# ============================================================================================================ servers
def _server_from(body: ServerIn | ServerPatch, srv: Server, runtime: Any) -> None:
    data = body.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    host_info = data.pop("host_info", None)
    data.pop("id", None)
    if "platform" in data and data["platform"] not in PLATFORMS:
        raise HTTPException(400, f"Unknown platform; use one of {', '.join(PLATFORMS)}.")
    for k, v in data.items():
        setattr(srv, k, v)
    if password is not None:
        srv.password_enc = runtime.keyring.encrypt(password) if password else None
    if host_info is not None:
        info = dict(srv.info or {})
        info["host"] = {k: v for k, v in host_info.items() if v is not None}
        srv.info = info


def _demo_candidate(runtime: Any, host: str | None) -> Any:
    """In the demo, the simulated server that onboarding can connect to (nothing real is contacted)."""
    if runtime.fleet is None or not host:
        return None
    from ..demo.profiles import candidate_for

    return candidate_for(host)


@router.get("/servers")
def list_servers(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        return [serial.server(x) for x in s.execute(select(Server).order_by(Server.environment, Server.id)).scalars()]


@router.post("/servers")
def create_server(body: ServerIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    sid = (body.id or re.sub(r"[^a-z0-9-]+", "-", body.name.lower())).strip("-")[:63]
    if not SLUG.match(sid):
        raise HTTPException(400, "Use lowercase letters, digits and dashes for the server id (2-63 characters).")
    with runtime.db.session() as s:
        if s.get(Server, sid):
            raise HTTPException(409, f"A server with the id {sid} already exists.")
        slot = (s.execute(select(func.max(Server.color_slot))).scalar() or -1) + 1
        srv = Server(id=sid, name=body.name, color_slot=slot % 8, info={})
        _server_from(body, srv, runtime)
        if (cand := _demo_candidate(runtime, srv.host)) is not None:
            srv.info = {**(srv.info or {}), "simulated": cand.id}
        s.add(srv)
        s.flush()
        audit(s, who.email, "server.create", sid, "allow", platform=srv.platform, environment=srv.environment, host=srv.host)
        return serial.server(srv)


@router.get("/servers/{sid}")
def get_server(sid: str, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        srv = s.get(Server, sid)
        if srv is None:
            raise HTTPException(404, "No such server.")
        out = serial.server(srv)
        out["chips"] = _chips(s, sid)
        out["suggestions"] = [serial.suggestion(x) for x in s.execute(select(Suggestion).where(Suggestion.server_id == sid, Suggestion.status == "open")
                                                                      .order_by(Suggestion.kind, Suggestion.id)).scalars()]
        out["incidents"] = [serial.incident_card(i, srv.name) for i in s.execute(select(Incident).where(Incident.server_id == sid)
                                                                                  .order_by(Incident.number.desc()).limit(12)).scalars()]
        last = s.execute(select(Assessment).where(Assessment.server_id == sid).order_by(Assessment.id.desc()).limit(1)).scalar_one_or_none()
        out["assessment"] = serial.assessment(last, full=False) if last else None
        out["firing"] = [{"monitor": m.name, "severity": m.severity, "since": iso(st.since), "value": st.value}
                         for st in s.execute(select(MonitorState).where(MonitorState.server_id == sid, MonitorState.state == "firing")).scalars()
                         if (m := s.get(Monitor, st.monitor_id)) is not None]
    out["metrics"] = {k: round(v, 3) for k, v in _latest(runtime, sid).items()}
    return out


@router.patch("/servers/{sid}")
def patch_server(sid: str, body: ServerPatch, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        srv = s.get(Server, sid)
        if srv is None:
            raise HTTPException(404, "No such server.")
        _server_from(body, srv, runtime)
        audit(s, who.email, "server.update", sid, "allow", fields=sorted(k for k in body.model_dump(exclude_unset=True) if k != "password"))
        return serial.server(srv)


@router.delete("/servers/{sid}")
def delete_server(sid: str, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, bool]:
    with runtime.db.session() as s:
        srv = s.get(Server, sid)
        if srv is None:
            raise HTTPException(404, "No such server.")
        audit(s, who.email, "server.delete", sid, "allow")
        s.delete(srv)
    runtime.store.forget(sid)
    return {"ok": True}


@router.post("/servers/test")
async def test_connection(body: ConnTest, runtime: Any = Depends(rt), _: User = Depends(admin)) -> dict[str, Any]:
    password = body.password
    if password is None and body.server_id:
        with runtime.db.session() as s:
            srv = s.get(Server, body.server_id)
            password = runtime.keyring.decrypt(srv.password_enc) if srv else None
    drv: Any = PostgresDriver("test", {"host": body.host, "port": body.port, "dbname": body.dbname, "user": body.username, "password": password, "sslmode": body.sslmode})
    if (cand := _demo_candidate(runtime, body.host)) is not None:
        from ..demo.driver import DemoDriver

        drv = DemoDriver("test", runtime.fleet, cand.id)
    try:
        return await asyncio.wait_for(drv.test(), timeout=20)
    except asyncio.TimeoutError:
        return {"ok": False, "message": "No answer within 20 s: check the host, the port and the firewall.", "checks": []}


@router.post("/servers/{sid}/test")
async def test_server(sid: str, runtime: Any = Depends(rt), _: User = Depends(operator)) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(runtime.driver(sid).test(), timeout=20)
    except DriverError as exc:
        return {"ok": False, "message": str(exc), "checks": []}
    except asyncio.TimeoutError:
        return {"ok": False, "message": "No answer within 20 s.", "checks": []}


@router.post("/servers/{sid}/agent-token")
def agent_token(sid: str, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    token, digest = new_agent_token()
    with runtime.db.session() as s:
        srv = s.get(Server, sid)
        if srv is None:
            raise HTTPException(404, "No such server.")
        srv.agent_token_hash = digest
        audit(s, who.email, "server.agent_token", sid, "allow")
    return {"token": token, "env": f"RAIDEN_CONSOLE_URL={runtime.settings.base_url or 'https://<console>'}\nRAIDEN_CONSOLE_TOKEN={token}\nRAIDEN_SERVER_ID={sid}"}


@router.put("/servers/{sid}/autonomy")
def server_autonomy(sid: str, body: dict[str, str | None], runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    from ..policy import GATED, MODES

    with runtime.db.session() as s:
        srv = s.get(Server, sid)
        if srv is None:
            raise HTTPException(404, "No such server.")
        cur = dict(srv.autonomy or {})
        for stage, mode in body.items():
            if stage not in GATED:
                raise HTTPException(400, f"Unknown stage {stage}.")
            if mode is None:
                cur.pop(stage, None)
            elif mode in MODES:
                cur[stage] = mode
            else:
                raise HTTPException(400, "Mode must be auto, human or null (inherit).")
        srv.autonomy = cur
        audit(s, who.email, "autonomy.server", sid, "allow", **body)
        return serial.server(srv)


# ============================================================================================================ metrics
@router.get("/servers/{sid}/metrics")
def server_metrics(sid: str, keys: str = Query(...), range: str = "1h", agg: str = "avg", points: int = Query(240, ge=10, le=1000),
                   runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    span = RANGES.get(range, 3600)
    end = int(time.time())
    wanted = [k for k in keys.split(",") if k][:24]
    latest = _latest(runtime, sid)
    expanded: list[str] = []
    for k in wanted:
        if k.endswith("[*]"):
            base = k[:-3]
            expanded += sorted(x for x in latest if x.startswith(base + "["))
        else:
            expanded.append(k)
    series = runtime.store.series(sid, expanded, end - span, end, max_points=points, agg=agg)
    return {"server_id": sid, "range": range, "start": end - span, "end": end, "series": series, "latest": {k: latest.get(k) for k in expanded}}


@router.get("/servers/{sid}/metric-keys")
def metric_keys(sid: str, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[str]:
    return runtime.store.keys_for(sid)


@router.get("/metrics/fleet")
def fleet_metric(metric: str, range: str = "6h", servers: str = "", agg: str = "avg", runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    if split_key(metric)[0] not in BY_ID:
        raise HTTPException(400, "Unknown metric.")
    span = RANGES.get(range, 6 * 3600)
    end = int(time.time())
    with runtime.db.session() as s:
        rows = list(s.execute(select(Server).order_by(Server.environment, Server.id)).scalars())
    pick = set(servers.split(",")) if servers else None
    out = []
    for srv in rows:
        if pick and srv.id not in pick:
            continue
        pts = runtime.store.series(srv.id, [metric], end - span, end, max_points=240, agg=agg)[metric]
        if pts:
            out.append({"server_id": srv.id, "name": srv.name, "environment": srv.environment, "color_slot": srv.color_slot, "points": pts})
    return {"metric": metric, "range": range, "start": end - span, "end": end, "series": out}


# ============================================================================================================ live drill-downs & actions
@router.get("/servers/{sid}/live/{kind}")
async def live(sid: str, kind: str, state: str | None = None, role: str | None = None, order_by: str = "total_time", database: str | None = None,
               pattern: str | None = None, pending: bool = False, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    if kind not in LIVE:
        raise HTTPException(404, "Unknown view.")
    drv = runtime.driver(sid)
    try:
        if kind == "overview":
            return await drv.overview()
        if kind == "sessions":
            return await drv.sessions(state, 0, role, 200)
        if kind == "locks":
            return await drv.lock_tree()
        if kind == "queries":
            return await drv.top_queries(order_by, 25)
        if kind == "tables":
            return await drv.table_health(database, 40)
        if kind == "replication":
            return await drv.replication()
        if kind == "settings":
            return await drv.settings(None, pattern, pending)
        if kind == "databases":
            return await drv.databases()
        return await drv.wraparound()
    except DriverError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/servers/{sid}/actions")
async def server_action(sid: str, body: ActionIn, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    """A person runs an action directly (from the sessions view or an assessment finding): they are the approval."""
    if body.action not in ACTIONS:
        raise HTTPException(400, "Unknown action.")
    try:
        res = await runtime.driver(sid).execute_action(body.action, body.args)
        ok, message = True, res.get("message", "done")
    except DriverError as exc:
        ok, message = False, str(exc)
    with runtime.db.session() as s:
        audit(s, who.email, f"action.{body.action}", sid, "allow" if ok else "error", args=body.args, reason=body.reason, result=message[:300])
    if not ok:
        raise HTTPException(400, message)
    runtime.bus.publish("metrics", server=sid)
    return {"ok": ok, "message": message}


# ============================================================================================================ assessments
@router.post("/servers/{sid}/assess")
async def assess(sid: str, trigger: str = "manual", runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        if s.get(Server, sid) is None:
            raise HTTPException(404, "No such server.")
    task = asyncio.create_task(runtime.assess(sid, trigger=trigger if trigger in ("onboarding", "manual") else "manual", user=who.email))
    await asyncio.sleep(0.05)
    with runtime.db.session() as s:
        a = s.execute(select(Assessment).where(Assessment.server_id == sid).order_by(Assessment.id.desc()).limit(1)).scalar_one_or_none()
        audit(s, who.email, "server.assess", sid, "allow")
        return {"id": a.id if a else None, "status": "running", "pending": not task.done()}


@router.get("/servers/{sid}/assessments")
def assessments(sid: str, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        return [serial.assessment(a, full=False) for a in s.execute(select(Assessment).where(Assessment.server_id == sid).order_by(Assessment.id.desc()).limit(20)).scalars()]


@router.get("/assessments/{aid}")
def get_assessment(aid: int, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        a = s.get(Assessment, aid)
        if a is None:
            raise HTTPException(404, "No such assessment.")
        return serial.assessment(a)


@router.post("/suggestions/{xid}/dismiss")
def dismiss(xid: int, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        x = s.get(Suggestion, xid)
        if x is None:
            raise HTTPException(404, "No such suggestion.")
        x.status = "dismissed"
        audit(s, who.email, "suggestion.dismiss", x.server_id, "allow", title=x.title)
        return serial.suggestion(x)
