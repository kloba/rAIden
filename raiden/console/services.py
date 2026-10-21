"""Small shared operations on the console's state: settings, audit, incidents and their timeline, suggestions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import iso, utcnow
from .models import AuditEntry, Incident, IncidentEvent, Server, Setting, Suggestion

OPEN_STAGES = ("detected", "triage", "diagnose", "remediate", "verify", "harden")
SEVERITY_ORDER = {"sev1": 1, "sev2": 2, "sev3": 3, "sev4": 4}


# ---------------------------------------------------------------------------------------------------- settings
def get_setting(s: Session, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    row = s.get(Setting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else dict(default or {})


def put_setting(s: Session, key: str, value: dict[str, Any], by: str = "system") -> None:
    row = s.get(Setting, key)
    if row is None:
        s.add(Setting(key=key, value=value, updated_by=by))
    else:
        row.value = value
        row.updated_by = by
        row.updated_at = utcnow()


# ---------------------------------------------------------------------------------------------------- audit
def audit(s: Session, actor: str, action: str, target: str = "", decision: str = "", actor_type: str = "user", **detail: Any) -> None:
    s.add(AuditEntry(actor=actor, actor_type=actor_type, action=action, target=target, decision=decision, detail=detail))


# ---------------------------------------------------------------------------------------------------- incidents
def next_incident_number(s: Session) -> int:
    top = s.execute(select(func.max(Incident.number))).scalar()
    return (top or 1000) + 1


def open_incident_for(s: Session, fingerprint: str) -> Incident | None:
    return s.execute(
        select(Incident).where(Incident.fingerprint == fingerprint, Incident.stage.in_(OPEN_STAGES)).order_by(Incident.number.desc()).limit(1)
    ).scalar_one_or_none()


def open_incidents_on(s: Session, server_id: str) -> list[Incident]:
    return list(s.execute(select(Incident).where(Incident.server_id == server_id, Incident.stage.in_(OPEN_STAGES))).scalars())


def create_incident(
    s: Session, *, server_id: str | None, title: str, severity: str, category: str, source: str, source_ref: str = "",
    fingerprint: str = "", summary: str = "", evidence: dict[str, Any] | None = None, scenario: str = "", actor: str = "raiden",
    detected_at: datetime | None = None,
) -> Incident:
    number = next_incident_number(s)
    now = detected_at or utcnow()
    inc = Incident(
        id=f"INC-{number}", number=number, server_id=server_id, title=title[:300], summary=summary, severity=severity, category=category,
        source=source, source_ref=source_ref, fingerprint=fingerprint, stage="detected", state="queued", evidence=evidence or {},
        scenario=scenario, detected_at=now, stage_times={"detected": iso(now)},
    )
    s.add(inc)
    s.flush()
    add_event(s, inc, "detected", f"Detected by {_source_label(source, source_ref)}", summary, actor=actor if source in ("manual", "chat") else "system",
              stage="detected", data=evidence or {}, ts=now)
    return inc


def _source_label(source: str, ref: str) -> str:
    return {"monitor": f"monitor {ref}", "routine": f"routine {ref}", "chat": "a chat investigation", "host_agent": "the host agent",
            "manual": "a person", "log": "the log watcher"}.get(source, source)


def add_event(
    s: Session, inc: Incident, kind: str, title: str, body: str = "", actor: str = "raiden", stage: str | None = None,
    data: dict[str, Any] | None = None, ts: datetime | None = None,
) -> IncidentEvent:
    ev = IncidentEvent(incident_id=inc.id, kind=kind, title=title[:400], body=body or "", actor=actor, stage=stage or inc.stage, data=data or {},
                       ts=ts or utcnow())
    s.add(ev)
    inc.updated_at = utcnow()
    return ev


def set_stage(s: Session, inc: Incident, stage: str, actor: str = "raiden", note: str = "") -> None:
    if inc.stage == stage:
        return
    prev = inc.stage
    inc.stage = stage
    now = utcnow()
    times = dict(inc.stage_times or {})
    times[stage] = iso(now)
    inc.stage_times = times
    if stage == "triage" and inc.acknowledged_at is None:
        inc.acknowledged_at = now
    if stage == "verify" and inc.mitigated_at is None:
        inc.mitigated_at = now
    if stage == "resolved":
        inc.resolved_at = now
        inc.state = "done"
        after_commit(s, "resolved", incident_id=inc.id)
    add_event(s, inc, "stage", f"{prev.title()} → {stage.title()}", note, actor=actor, stage=stage, data={"from": prev, "to": stage})


def after_commit(s: Session, kind: str, **data: Any) -> None:
    """A notification to send once this session's changes are stored (Database.on_commit delivers it)."""
    s.info.setdefault("after_commit", []).append((kind, data))


# ---------------------------------------------------------------------------------------------------- suggestions
def upsert_suggestion(
    s: Session, server_id: str, *, kind: str, category: str, title: str, detail: str, source: str, fingerprint: str, data: dict[str, Any] | None = None,
) -> Suggestion:
    row = s.execute(select(Suggestion).where(Suggestion.server_id == server_id, Suggestion.fingerprint == fingerprint)).scalar_one_or_none()
    if row is None:
        row = Suggestion(server_id=server_id, kind=kind, category=category, title=title, detail=detail, source=source, fingerprint=fingerprint, data=data or {})
        s.add(row)
    else:
        if row.status == "dismissed":
            return row  # stays as the person left it
        if row.status == "resolved":
            row.status = "open"
        row.kind, row.category, row.title, row.detail, row.data = kind, category, title, detail, data or {}
        row.updated_at = utcnow()
    return row


def resolve_suggestion(s: Session, server_id: str, fingerprint: str) -> None:
    row = s.execute(select(Suggestion).where(Suggestion.server_id == server_id, Suggestion.fingerprint == fingerprint)).scalar_one_or_none()
    if row and row.status == "open":
        row.status = "resolved"
        row.updated_at = utcnow()


def server_label(s: Session, server_id: str | None) -> str:
    if not server_id:
        return "the fleet"
    srv = s.get(Server, server_id)
    return srv.name if srv else server_id
