"""Incidents, approvals and the autonomy policy."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from ..db import utcnow
from ..models import AgentSession, Approval, Incident, IncidentEvent, Server, User
from ..policy import ENVIRONMENTS, GATED, MODES, STAGES, describe, merged
from ..services import OPEN_STAGES, add_event, audit, create_incident, get_setting, put_setting
from . import serial
from .deps import admin, operator, rt, viewer

router = APIRouter(prefix="/api/v1")


class NewIncident(BaseModel):
    server_id: str | None = None
    title: str = Field(min_length=3, max_length=300)
    severity: str = Field(default="sev3", pattern="^sev[1-4]$")
    summary: str = ""


class Move(BaseModel):
    stage: str


class Note(BaseModel):
    text: str = Field(default="", max_length=4000)


class Decision(BaseModel):
    approve: bool
    comment: str = Field(default="", max_length=2000)


class PolicyIn(BaseModel):
    environments: dict[str, dict[str, str]] | None = None
    high_risk_always_human: bool | None = None
    verify_window_s: int | None = Field(default=None, ge=15, le=3600)
    max_auto_actions: int | None = Field(default=None, ge=1, le=50)
    budget_exhausted: str | None = Field(default=None, pattern="^(human)$")


def _names(s: Any) -> dict[str, str]:
    return {x.id: x.name for x in s.execute(select(Server)).scalars()}


@router.get("/incidents")
def list_incidents(status: str = "board", server: str | None = None, q: str | None = None, limit: int = 200,
                   runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        stmt = select(Incident).order_by(Incident.number.desc()).limit(min(limit, 500))
        if status == "open":
            stmt = stmt.where(Incident.stage.in_(OPEN_STAGES))
        elif status == "resolved":
            stmt = stmt.where(Incident.stage == "resolved")
        elif status == "board":  # open ones plus the last resolved ones
            recent = utcnow().replace(microsecond=0)
            from datetime import timedelta

            stmt = stmt.where(or_(Incident.stage.in_(OPEN_STAGES), Incident.resolved_at >= recent - timedelta(days=2)))
        if server:
            stmt = stmt.where(Incident.server_id == server)
        if q:
            stmt = stmt.where(or_(Incident.title.ilike(f"%{q}%"), Incident.id.ilike(f"%{q}%")))
        incs = list(s.execute(stmt).scalars())
        pending: dict[str, list[Approval]] = {}
        for a in s.execute(select(Approval).where(Approval.status == "pending", Approval.incident_id.isnot(None))).scalars():
            pending.setdefault(a.incident_id, []).append(a)
        names = _names(s)
        return [serial.incident_card(i, names.get(i.server_id or ""), pending.get(i.id)) for i in incs]


@router.get("/incidents/{iid}")
def get_incident(iid: str, runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "No such incident.")
        events = list(s.execute(select(IncidentEvent).where(IncidentEvent.incident_id == iid).order_by(IncidentEvent.ts, IncidentEvent.id)).scalars())
        approvals = list(s.execute(select(Approval).where(Approval.incident_id == iid).order_by(Approval.id)).scalars())
        out = serial.incident_detail(inc, _names(s).get(inc.server_id or ""), events, approvals)
        sess = s.execute(select(AgentSession).where(AgentSession.incident_id == iid)).scalar_one_or_none()
        out["session"] = {"turns": sess.turns, "tool_calls": sess.tool_calls, "model": sess.model, "status": sess.status} if sess else None
        srv = s.get(Server, inc.server_id) if inc.server_id else None
        if srv:
            policy = runtime.pipeline.policy(s)
            from ..policy import mode_for

            out["autonomy"] = {st: mode_for(policy, srv.environment, srv.autonomy, st) for st in GATED}
            out["environment"] = srv.environment
            out["platform"] = srv.platform
        return out


@router.post("/incidents")
def new_incident(body: NewIncident, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        if body.server_id and s.get(Server, body.server_id) is None:
            raise HTTPException(404, "No such server.")
        inc = create_incident(s, server_id=body.server_id, title=body.title, severity=body.severity, category="other", source="manual", source_ref=who.email,
                              fingerprint=f"manual:{who.email}:{utcnow().timestamp()}", summary=body.summary, actor=who.email,
                              scenario=runtime.demo_scenario(body.server_id) if body.server_id else "")
        audit(s, who.email, "incident.create", inc.id, "allow", title=body.title)
        out = serial.incident_card(inc)
    runtime.bus.publish("incident", id=out["id"])
    return out


@router.post("/incidents/{iid}/move")
def move(iid: str, body: Move, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, bool]:
    if body.stage not in STAGES or body.stage == "detected":
        raise HTTPException(400, "Unknown stage.")
    runtime.pipeline.move(iid, body.stage, who.email)
    return {"ok": True}


@router.post("/incidents/{iid}/take-over")
def take_over(iid: str, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, bool]:
    runtime.pipeline.take_over(iid, who.email)
    return {"ok": True}


@router.post("/incidents/{iid}/hand-back")
def hand_back(iid: str, body: Note, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, bool]:
    runtime.pipeline.hand_back(iid, who.email, body.text)
    return {"ok": True}


@router.post("/incidents/{iid}/comment")
def comment(iid: str, body: Note, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    with runtime.db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "No such incident.")
        ev = add_event(s, inc, "comment", f"{who.name or who.email} commented", body.text, actor=who.email)
        sess = s.execute(select(AgentSession).where(AgentSession.incident_id == iid)).scalar_one_or_none()
        if sess is not None and inc.stage in OPEN_STAGES:  # rAIden reads comments at its next turn
            sess.messages = list(sess.messages) + [{"role": "user", "content": f"Note from {who.email} (a person on the team): {body.text}"}]
        s.flush()
        out = serial.event(ev)
    runtime.bus.publish("incident", id=iid)
    return out


@router.post("/incidents/{iid}/resolve")
def resolve(iid: str, body: Note, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, bool]:
    with runtime.db.session() as s:
        inc = s.get(Incident, iid)
        if inc is None:
            raise HTTPException(404, "No such incident.")
        if body.text:
            inc.summary = body.text
    runtime.pipeline.move(iid, "resolved", who.email)
    return {"ok": True}


# ============================================================================================================ approvals
@router.get("/approvals")
def approvals(status: str = "pending", runtime: Any = Depends(rt), _: User = Depends(viewer)) -> list[dict[str, Any]]:
    with runtime.db.session() as s:
        stmt = select(Approval).order_by(Approval.requested_at.desc()).limit(200)
        if status != "all":
            stmt = stmt.where(Approval.status == status)
        rows = list(s.execute(stmt).scalars())
        names = _names(s)
        incs = {i.id: i for i in s.execute(select(Incident).where(Incident.id.in_([a.incident_id for a in rows if a.incident_id]))).scalars()}
        out = []
        for a in rows:
            inc = incs.get(a.incident_id or "")
            out.append({**serial.approval(a), "server_name": names.get(a.server_id or "", a.server_id), "incident_title": inc.title if inc else None,
                        "severity": inc.severity if inc else None, "incident_stage": inc.stage if inc else None})
        return out


@router.post("/approvals/{aid}/decide")
async def decide(aid: int, body: Decision, runtime: Any = Depends(rt), who: User = Depends(operator)) -> dict[str, Any]:
    try:
        res = runtime.pipeline.decide(aid, body.approve, who.email, body.comment)
    except KeyError:
        raise HTTPException(404, "No such approval.") from None
    except ValueError as exc:
        raise HTTPException(409, f"This decision was already made ({exc}).") from None
    if res.get("conversation_id"):
        asyncio.create_task(runtime.chat.after_decision(aid))
    return res


# ============================================================================================================ autonomy policy
@router.get("/autonomy")
def get_policy(runtime: Any = Depends(rt), _: User = Depends(viewer)) -> dict[str, Any]:
    with runtime.db.session() as s:
        policy = merged(get_setting(s, "autonomy"))
        overrides = [{"id": x.id, "name": x.name, "environment": x.environment, "autonomy": x.autonomy}
                     for x in s.execute(select(Server).order_by(Server.id)).scalars() if x.autonomy]
    return {**describe(policy), "overrides": overrides}


@router.put("/autonomy")
def put_policy(body: PolicyIn, runtime: Any = Depends(rt), who: User = Depends(admin)) -> dict[str, Any]:
    with runtime.db.session() as s:
        cur = merged(get_setting(s, "autonomy"))
        if body.environments:
            for env, stages in body.environments.items():
                if env not in ENVIRONMENTS:
                    raise HTTPException(400, f"Unknown environment {env}.")
                for st, mode in stages.items():
                    if st not in GATED or mode not in MODES:
                        raise HTTPException(400, f"Bad stage/mode {st}={mode}.")
                    if cur["environments"][env].get(st) != mode:
                        audit(s, who.email, "autonomy.update", env, "allow", stage=st, mode=mode)
                    cur["environments"][env][st] = mode
        for k in ("high_risk_always_human", "verify_window_s", "max_auto_actions", "budget_exhausted"):
            v = getattr(body, k)
            if v is not None:
                if cur.get(k) != v:
                    audit(s, who.email, "autonomy.update", k, "allow", value=v)
                cur[k] = v
        put_setting(s, "autonomy", cur, by=who.email)
    runtime.bus.publish("autonomy")
    return get_policy(runtime, who)
