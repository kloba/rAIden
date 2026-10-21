"""The console's own state: servers, metrics, monitors, incidents, approvals, AI sessions, routines, endpoints, audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, utcnow

BigId = BigInteger().with_variant(Integer, "sqlite")  # SQLite only auto-increments INTEGER PRIMARY KEY


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin | operator | viewer
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)  # bump to sign out every session
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # slug, e.g. orders-prod
    name: Mapped[str] = mapped_column(String(120))
    environment: Mapped[str] = mapped_column(String(20), default="production")  # production | staging | development
    platform: Mapped[str] = mapped_column(String(32), default="self_managed")
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=5432)
    dbname: Mapped[str] = mapped_column(String(120), default="postgres")
    username: Mapped[str] = mapped_column(String(120), default="raiden_monitor")
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    sslmode: Mapped[str] = mapped_column(String(20), default="prefer")
    databases: Mapped[list[Any]] = mapped_column(JSON, default=list)  # databases for per-database metrics; [] = discover
    azure_resource_id: Mapped[str | None] = mapped_column(String(400), nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    owner: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    maintenance_window: Mapped[str] = mapped_column(String(120), default="")
    collect_interval_s: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    autonomy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # stage -> auto|human, overrides the environment
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # healthy | warning | critical | unreachable | paused | unknown
    status_reason: Mapped[str] = mapped_column(String(300), default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # version, capabilities, region, sku, uptime ...
    color_slot: Mapped[int] = mapped_column(Integer, default=0)  # stable chart color: color follows the server
    agent_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MetricSnapshot(Base):
    """One collection of every metric of one server: {metric: value, "metric[dim]": value}."""

    __tablename__ = "metric_snapshots"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    server_id: Mapped[str] = mapped_column(String(64), ForeignKey("servers.id", ondelete="CASCADE"))
    ts: Mapped[int] = mapped_column(Integer)  # epoch seconds
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (Index("ix_snapshots_server_ts", "server_id", "ts"),)


class MetricRollup(Base):
    """15-minute buckets kept for the long view: {metric: [avg, max, min]}."""

    __tablename__ = "metric_rollups"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    server_id: Mapped[str] = mapped_column(String(64), ForeignKey("servers.id", ondelete="CASCADE"))
    ts: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    __table_args__ = (UniqueConstraint("server_id", "ts", name="uq_rollups_server_ts"),)


class Monitor(Base):
    """A metric rule. When it holds for the window, rAIden reacts: open an incident, or add a suggestion."""

    __tablename__ = "monitors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)  # built-in monitors have a key
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    metric: Mapped[str] = mapped_column(String(80))
    dimension: Mapped[str | None] = mapped_column(String(120), nullable=True)
    aggregation: Mapped[str] = mapped_column(String(10), default="avg")  # avg | max | min | last
    operator: Mapped[str] = mapped_column(String(4), default=">")
    threshold: Mapped[float] = mapped_column(Float)
    window_minutes: Mapped[int] = mapped_column(Integer, default=5)
    severity: Mapped[str] = mapped_column(String(8), default="sev3")
    category: Mapped[str] = mapped_column(String(32), default="performance")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"environments": [], "servers": [], "tags": {}}
    action: Mapped[str] = mapped_column(String(16), default="investigate")  # investigate | notify | suggest
    runbook: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(200), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MonitorState(Base):
    __tablename__ = "monitor_states"
    monitor_id: Mapped[int] = mapped_column(Integer, ForeignKey("monitors.id", ondelete="CASCADE"), primary_key=True)
    server_id: Mapped[str] = mapped_column(String(64), ForeignKey("servers.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(10), default="ok")  # ok | firing | nodata
    since: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    incident_id: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(20), primary_key=True)  # INC-1042
    number: Mapped[int] = mapped_column(Integer, unique=True)
    server_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("servers.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(8), default="sev3")
    category: Mapped[str] = mapped_column(String(32), default="other")
    source: Mapped[str] = mapped_column(String(20), default="monitor")  # monitor | routine | chat | host_agent | manual
    source_ref: Mapped[str] = mapped_column(String(200), default="")
    fingerprint: Mapped[str] = mapped_column(String(200), default="", index=True)
    stage: Mapped[str] = mapped_column(String(12), default="detected")
    step: Mapped[str] = mapped_column(String(16), default="")  # a sub-step inside a stage: "check" (verify), "report" (harden)
    state: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | waiting | idle | escalated | done
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # how often verification failed and the incident went back to diagnose
    assignee: Mapped[str] = mapped_column(String(200), default="raiden")
    waiting_reason: Mapped[str] = mapped_column(Text, default="")
    hypothesis: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    plan: Mapped[list[Any]] = mapped_column(JSON, default=list)
    hardening: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    report: Mapped[str] = mapped_column(Text, default="")
    scenario: Mapped[str] = mapped_column(String(40), default="")  # demo playbook hint
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(120), default="")
    recurrence_of: Mapped[str | None] = mapped_column(String(20), nullable=True)
    verify_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stage_times: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # stage -> ISO time entered
    labels: Mapped[list[Any]] = mapped_column(JSON, default=list)
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mitigated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (Index("ix_incidents_stage", "stage"), Index("ix_incidents_server", "server_id"))


class IncidentEvent(Base):
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(20), ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(200), default="raiden")
    stage: Mapped[str] = mapped_column(String(12), default="")
    title: Mapped[str] = mapped_column(String(400), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    server_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(12))  # gate | action | hardening
    stage: Mapped[str] = mapped_column(String(12), default="")
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    risk: Mapped[str] = mapped_column(String(8), default="medium")
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | approved | rejected | cancelled
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")


class AgentSession(Base):
    """A durable AI conversation: messages are persisted after every turn, so it survives restarts and long approvals."""

    __tablename__ = "agent_sessions"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(12))  # incident | chat | routine
    incident_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    routine_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="idle")  # idle | running | paused | done | error
    cursor: Mapped[str] = mapped_column(String(40), default="")  # the stage/step whose instructions were last given
    messages: Mapped[list[Any]] = mapped_column(JSON, default=list)
    pending: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="New investigation")
    server_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_email: Mapped[str] = mapped_column(String(200), default="")
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    incident_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(40), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(12))  # user | assistant | tool | approval | error
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Routine(Base):
    __tablename__ = "routines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(8), default="check")  # check (deterministic SQL) | agent (AI session)
    checks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    prompt: Mapped[str] = mapped_column(Text, default="")
    cron: Mapped[str] = mapped_column(String(80), default="*/15 * * * *")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    on_finding: Mapped[str] = mapped_column(String(12), default="suggest")  # suggest | incident | report
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(8), default="")
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RoutineRun(Base):
    __tablename__ = "routine_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    routine_id: Mapped[int] = mapped_column(Integer, ForeignKey("routines.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="running")  # running | ok | warn | crit | error
    summary: Mapped[str] = mapped_column(Text, default="")
    results: Mapped[list[Any]] = mapped_column(JSON, default=list)
    report: Mapped[str] = mapped_column(Text, default="")
    triggered_by: Mapped[str] = mapped_column(String(200), default="schedule")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class Suggestion(Base):
    """Issues (something is wrong) and suggestions (something could be better), shown as chips on the estate."""

    __tablename__ = "suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[str] = mapped_column(String(64), ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(12), default="suggestion")  # issue | suggestion
    category: Mapped[str] = mapped_column(String(32), default="performance")
    title: Mapped[str] = mapped_column(String(300))
    detail: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(80), default="")
    fingerprint: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | dismissed | resolved
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("server_id", "fingerprint", name="uq_suggestions_server_fp"),)


class Assessment(Base):
    """A health and configuration review of one server: onboarding baseline, or a re-run from the server page / a routine."""

    __tablename__ = "assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[str] = mapped_column(String(64), ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="running")  # running | done | error
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # onboarding | manual | routine
    triggered_by: Mapped[str] = mapped_column(String(200), default="")
    score: Mapped[int] = mapped_column(Integer, default=0)
    scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    tuning: Mapped[list[Any]] = mapped_column(JSON, default=list)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class AIEndpoint(Base):
    __tablename__ = "ai_endpoints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(24))  # azure_openai | openai | anthropic | openai_compatible | demo
    base_url: Mapped[str] = mapped_column(String(400), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    api_version: Mapped[str] = mapped_column(String(40), default="")
    wire_api: Mapped[str] = mapped_column(String(12), default="auto")  # auto | chat | responses
    auth: Mapped[str] = mapped_column(String(12), default="api_key")  # entra | api_key | none
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning_effort: Mapped[str] = mapped_column(String(10), default="medium")
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=8000)
    price_in_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    price_out_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    price_cached_per_m: Mapped[float] = mapped_column(Float, default=0.0)
    daily_token_budget: Mapped[int | None] = mapped_column(Integer, nullable=True)
    monthly_cost_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    purposes: Mapped[list[Any]] = mapped_column(JSON, default=list)  # incidents | chat | routines | hardening
    priority: Mapped[int] = mapped_column(Integer, default=10)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)  # demo fleet: answered by the scripted demo model
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    last_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TokenUsage(Base):
    __tablename__ = "token_usage"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(Integer, ForeignKey("ai_endpoints.id", ondelete="CASCADE"))
    bucket: Mapped[datetime] = mapped_column(DateTime)  # start of the hour (UTC)
    purpose: Mapped[str] = mapped_column(String(16))
    server_id: Mapped[str] = mapped_column(String(64), default="")
    calls: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    tokens_cached: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (
        UniqueConstraint("endpoint_id", "bucket", "purpose", "server_id", name="uq_usage_bucket"),
        Index("ix_usage_bucket", "bucket"),
    )


class AuditEntry(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigId, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    actor_type: Mapped[str] = mapped_column(String(12), default="user")  # user | raiden | system | host_agent
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(String(200), default="")
    decision: Mapped[str] = mapped_column(String(12), default="")  # allow | deny | approval | ""
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(200), default="system")
