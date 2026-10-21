"""The incident pipeline: Detected → Triage → Diagnose → Remediate → Verify → Harden → Resolved.

rAIden does the work of every stage; the autonomy policy decides at each gate whether it carries on by itself or waits for
a person. Rules that hold whatever the policy says:

* the approved plan is the contract: in Remediate, an action that is not in the approved plan is a new decision;
* high-risk actions (drop a slot, roll back a prepared transaction, free SQL) always wait for a person (policy default);
* a plan longer than `max_auto_actions` always waits for a person;
* no AI endpoint (or budget) left means the incident waits for a person instead of failing silently.

A remediation gate a person would open anyway (an accepted learned approval, see learning.py) opens on its own, and says so.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from . import fmt, learning
from .agent.loop import AgentLoop, LoopResult
from .agent.prompts import PLATFORM_NOTES, STAGE, SYSTEM_INCIDENT, incident_context, plan_text
from .agent.tools import ACTION_NEEDS, ROUTINE_TEMPLATES, ToolContext, ToolOutcome, server_brief, tools_for
from .catalog import BY_ID
from .db import utcnow
from .drivers.base import ACTIONS, DriverError
from .models import AgentSession, Approval, Incident, IncidentEvent, LearnedApproval, Monitor, Server, User
from .policy import STAGE_INFO, mode_for
from .services import OPEN_STAGES, add_event, audit, get_setting, set_stage, upsert_suggestion

log = logging.getLogger("raiden.console.pipeline")

MAX_TURNS = {"triage": 8, "diagnose": 16, "remediate": 14, "verify": 8, "harden": 10, "report": 5}
GATE_TITLES = {
    "triage": "Acknowledge and let rAIden start",
    "diagnose": "Confirm the triage before rAIden investigates",
    "remediate": "Approve the remediation plan",
    "verify": "Sign off: the fix holds",
    "harden": "Approve the protections",
    "resolved": "Review the report and close",
}


def _plan_items(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, p in enumerate(plan or []):
        spec = ACTIONS.get(p.get("action", ""), {})
        out.append({"id": i + 1, "action": p.get("action"), "args": p.get("args") or {}, "rationale": p.get("rationale", ""),
                    "risk": spec.get("risk", "high"), "title": spec.get("title", p.get("action")), "status": "proposed"})
    return out


def _same_action(item: dict[str, Any], action: str, args: dict[str, Any]) -> bool:
    if item.get("action") != action:
        return False
    a, b = item.get("args") or {}, args or {}
    return all(str(b.get(k)) == str(v) for k, v in a.items()) and all(str(a.get(k)) == str(v) for k, v in b.items() if v not in (None, ""))


def decision_title(approve: bool, title: str) -> str:
    """'Approved the protections', not 'ada@... approved: Approve the protections': the timeline shows who decided next to it."""
    verb = "Approved" if approve else "Rejected"
    return f"{verb} {title[len('Approve '):]}" if title.startswith("Approve ") else f"{verb}: {title}"


class Pipeline:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.running: set[str] = set()
        self.retry_at: dict[str, float] = {}
        self.sem = asyncio.Semaphore(max(1, rt.settings.max_sessions))

    # ================================================================================================ scheduling
    def policy(self, s: Any) -> dict[str, Any]:
        from .policy import merged

        return merged(get_setting(s, "autonomy"))

    def mode(self, s: Any, inc: Incident, stage: str) -> str:
        srv = s.get(Server, inc.server_id) if inc.server_id else None
        return mode_for(self.policy(s), srv.environment if srv else "production", srv.autonomy if srv else None, stage)

    async def tick(self) -> None:
        now = utcnow()
        with self.rt.db.session() as s:
            for inc in s.execute(select(Incident).where(Incident.stage == "verify", Incident.state == "idle", Incident.verify_until <= now)).scalars():
                inc.step, inc.state = "check", "queued"
                add_event(s, inc, "verify", "Verification window over: rAIden checks the evidence", actor="system")
            queued = [i for i in s.execute(select(Incident.id).where(Incident.state == "queued", Incident.stage.in_(OPEN_STAGES))).scalars()]
        for iid in queued:
            if iid in self.running or self.retry_at.get(iid, 0) > time.time():
                continue
            self.running.add(iid)
            asyncio.create_task(self._work(iid))

    async def _work(self, iid: str) -> None:
        try:
            async with self.sem:
                await self.advance(iid)
        except Exception as exc:  # noqa: BLE001
            log.exception("pipeline failed for %s", iid)
            self.retry_at[iid] = time.time() + 60
            with self.rt.db.session() as s:
                inc = s.get(Incident, iid)
                if inc and inc.state == "running":
                    inc.state = "queued"
                    add_event(s, inc, "error", "rAIden hit an internal error; retrying in a minute", f"{exc.__class__.__name__}: {exc}", actor="system")
        finally:
            self.running.discard(iid)
            self.rt.bus.publish("incident", id=iid)

    async def advance(self, iid: str) -> None:
        for _ in range(16):
            with self.rt.db.session() as s:
                inc = s.get(Incident, iid)
                if inc is None or inc.state != "queued" or inc.stage not in OPEN_STAGES:
                    return
                if inc.stage == "detected":
                    self._gate(s, inc, "triage", material="")
                    continue
                stage, step = inc.stage, inc.step
                inc.state = "running"
                inc.assignee = "raiden"
            self.rt.bus.publish("incident", id=iid)
            if stage == "harden" and step == "apply":
                await self._apply_hardening(iid)
                continue
            result = await self._run(iid, stage, step)
            if not await self._handle(iid, stage, step, result):
                return

    # ================================================================================================ sessions
    def _session(self, s: Any, inc: Incident) -> AgentSession:
        sess = s.execute(select(AgentSession).where(AgentSession.incident_id == inc.id, AgentSession.kind == "incident")).scalar_one_or_none()
        if sess:
            return sess
        srv = s.get(Server, inc.server_id) if inc.server_id else None
        metrics = self.rt.current_metrics(inc.server_id)["rows"] if inc.server_id else []
        brief = server_brief(s, inc.server_id) if inc.server_id else "the fleet"
        system = SYSTEM_INCIDENT.format(server=brief, platform_note=PLATFORM_NOTES.get(srv.platform if srv else "", ""))
        ctx = incident_context(inc, brief, {r[0]: r[2] for r in metrics}, learning.prompt_block(s, inc.server_id, srv.environment if srv else "production"))
        sess = AgentSession(id=uuid.uuid4().hex, kind="incident", incident_id=inc.id, server_id=inc.server_id, status="idle",
                            messages=[{"role": "system", "content": system}, {"role": "user", "content": ctx}])
        s.add(sess)
        s.flush()
        return sess

    def _instructions(self, s: Any, inc: Incident, stage: str, step: str) -> str:
        if stage == "remediate":
            approver = inc.evidence.get("_plan_approved_by", "autopilot")
            return STAGE["remediate"].format(approved_by=f"by {approver}" if approver != "autopilot" else "autopilot", plan=plan_text(inc.plan))
        if stage == "verify":
            window = fmt.duration(self.policy(s)["verify_window_s"] / (self.rt.settings.demo_speed if self.rt.settings.demo else 1))
            sig = inc.evidence.get("metric")
            state = self._signal_state(s, inc)
            return STAGE["verify"].format(window=window, signal=f"{sig} ({inc.evidence.get('monitor', '')})" if sig else inc.source, signal_state=state)
        if stage == "harden" and step == "report":
            applied = [h for h in inc.hardening if h.get("status") == "applied"]
            handed = [h for h in inc.hardening if h.get("status") == "suggested"]
            failed = [h for h in inc.hardening if h.get("status") == "failed"]
            text = ("Protections added: " + "; ".join(h["title"] for h in applied) if applied else "No protections were added by rAIden.") + \
                   (" Left to a person (on the server's suggestions): " + "; ".join(h["title"] for h in handed) + "." if handed else "") + \
                   (" Failed: " + "; ".join(f"{h['title']} ({h.get('result', '')})" for h in failed) + "." if failed else "")
            # the console's own clock for the timeline: stage changes, decisions and actions are not in the conversation
            marks = [f"{e.ts:%H:%M:%S} {e.title}" for e in s.execute(select(IncidentEvent).where(
                IncidentEvent.incident_id == inc.id, IncidentEvent.kind.in_(("detected", "stage", "decision", "action", "signal", "verification", "hardened")))
                .order_by(IncidentEvent.id)).scalars()]
            text += "\nRecorded times (UTC, " + f"{inc.detected_at:%Y-%m-%d}" + "):\n" + "\n".join(f"- {m}" for m in marks)
            return STAGE["report"].format(hardening=text)
        if stage == "diagnose":  # triage named the kind of incident: what the team taught for it, and the playbook
            env = learning.environment_of(s, inc.server_id)
            extra = [x for x in (learning.prompt_block(s, inc.server_id, env, inc.category, only_category=True),
                                 learning.skill_block(s, inc.category, inc.server_id, env)) if x]
            return "\n\n".join([STAGE["diagnose"], *extra])
        return STAGE[stage]

    def _signal_state(self, s: Any, inc: Incident) -> str:
        metric = inc.evidence.get("metric")
        if not metric or not inc.server_id:
            return "unknown"
        latest = self.rt.store.latest_values(inc.server_id)
        v = latest.get(metric)
        mon_key = inc.evidence.get("monitor_key")
        mon = s.execute(select(Monitor).where(Monitor.key == mon_key)).scalar_one_or_none() if mon_key else None
        firing = None
        if mon is not None and v is not None:
            from .monitors import OPS

            firing = OPS.get(mon.operator, OPS[">"])(v, mon.threshold)
        return f"{fmt.value(metric, v)}" + ("" if firing is None else (" - STILL above the threshold" if firing else " - back to normal"))

    async def _run(self, iid: str, stage: str, step: str) -> LoopResult:
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            sess = self._session(s, inc)
            key = f"{stage}/{step}"
            if sess.cursor != key:
                msgs = list(sess.messages or [])
                msgs.append({"role": "user", "content": self._instructions(s, inc, stage, step)})
                sess.messages, sess.cursor = msgs, key
            session_id, server_id = sess.id, inc.server_id
            pending = dict(sess.pending or {}) if sess.pending else None
        mode = "report" if (stage == "harden" and step == "report") else stage
        ctx = ToolContext(self.rt, server_id, incident_id=iid, session_id=session_id, on_special=lambda n, a, t: self._special(iid, n, a, t))
        loop = AgentLoop(self.rt, session_id, "hardening" if stage == "harden" else "incidents", ctx, on_event=self._events(iid, mode))
        tools = tools_for(mode)
        if pending and pending.get("approval_id"):
            text = await self._decision_text(iid, int(pending["approval_id"]))
            if text is None:
                with self.rt.db.session() as s:
                    s.get(Incident, iid).state = "waiting"
                return LoopResult("paused")
            return await loop.resume(text, tools, MAX_TURNS[mode])
        return await loop.run(tools, MAX_TURNS[mode])

    def _events(self, iid: str, stage: str):
        def on_event(kind: str, data: dict[str, Any]) -> None:
            with self.rt.db.session() as s:
                inc = s.get(Incident, iid)
                if inc is None:
                    return
                if kind == "usage":
                    inc.tokens_in += data["tokens_in"]
                    inc.tokens_out += data["tokens_out"]
                    inc.tokens_cached += data["tokens_cached"]
                    inc.cost_usd = round(inc.cost_usd + data["cost_usd"], 6)
                    inc.model = data["model"]
                    return
                if kind == "thought":
                    add_event(s, inc, "thought", data["text"][:200], data["text"], stage=inc.stage)
                elif kind == "tool_result":
                    if data["name"] in ("submit_triage", "submit_diagnosis", "submit_remediation", "submit_verification", "propose_hardening",
                                        "submit_report", "escalate", "execute_action"):
                        return  # these get their own, richer events
                    inc.tool_calls += 1
                    add_event(s, inc, "tool", _tool_title(data["name"], data["args"]), "", stage=inc.stage,
                              data={"tool": data["name"], "args": data["args"], "ok": data["ok"], "ms": data["ms"], "preview": _trim_preview(data["preview"])})
            self.rt.bus.publish("incident", id=iid)
        return on_event

    # ================================================================================================ stage tools
    async def _special(self, iid: str, name: str, args: dict[str, Any], tool_call_id: str) -> ToolOutcome:
        if name == "execute_action":
            return await self._execute_action(iid, args, tool_call_id)
        if name == "submit_diagnosis":
            problems = []
            caps, platform = self._login(iid)
            for i, p in enumerate(args.get("plan") or []):
                spec = ACTIONS.get(p.get("action", ""))
                if spec is None:
                    problems.append(f"step {i + 1}: unknown action {p.get('action')!r}")
                    continue
                missing = [k for k in spec["required"] if (p.get("args") or {}).get(k) in (None, "")]
                if missing:
                    problems.append(f"step {i + 1} ({p['action']}): missing {', '.join(missing)}")
                elif why := cannot(p["action"], caps, platform):
                    problems.append(f"step {i + 1} ({p['action']}): {why}; plan what rAIden can do and name the rest in the root cause")
            if problems:
                return ToolOutcome(text="ERROR: the plan is not valid: " + "; ".join(problems) + ". Fix it and call submit_diagnosis again.", ok=False)
        if name == "propose_hardening":
            problems = self._hardening_problems(iid, args.get("items") or [])
            if problems:
                return ToolOutcome(text="ERROR: " + "; ".join(problems) + ". Fix the items and call propose_hardening again.", ok=False)
        return ToolOutcome(text="recorded", done={"tool": name, "args": args})

    def _login(self, iid: str) -> tuple[dict[str, Any], str]:
        """What rAIden's login may do on the incident's server, and the platform."""
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            srv = s.get(Server, inc.server_id) if inc and inc.server_id else None
            return (dict((srv.info or {}).get("capabilities") or {}) if srv else {}), (srv.platform if srv else "")

    def _hardening_problems(self, iid: str, items: list[dict[str, Any]]) -> list[str]:
        """What would fail when the protections are applied, said while the model can still fix its proposal."""
        caps, platform = self._login(iid)
        problems = []
        for it in items:
            kind, spec, title = it.get("kind"), it.get("spec") or {}, it.get("title") or "?"
            if kind == "monitor":
                problems += [f"{title!r}: {p}" for p in monitor_spec_problems(spec)]
            elif kind == "routine" and spec.get("template") not in ROUTINE_TEMPLATES:
                problems.append(f"{title!r}: unknown routine template {spec.get('template')!r}, use one of {', '.join(ROUTINE_TEMPLATES)} "
                                "(a change a person must make is a recommendation)")
            elif kind == "action":
                action = spec.get("action", "")
                if action not in ACTIONS:
                    problems.append(f"{title!r}: unknown action {action!r}")
                elif missing := [k for k in ACTIONS[action]["required"] if (spec.get("args") or {}).get(k) in (None, "")]:
                    problems.append(f"{title!r}: {action} needs {', '.join(missing)}")
                elif why := cannot(action, caps, platform):
                    problems.append(f"{title!r}: {why}: make it a recommendation")
            elif kind == "recommendation" and not str(spec.get("how") or "").strip():
                problems.append(f"{title!r}: a recommendation needs spec.how, the exact SQL or steps for the person")
            elif kind not in ("monitor", "routine", "action", "lesson", "recommendation"):
                problems.append(f"{title!r}: unknown kind {kind!r}")
        return problems

    async def _execute_action(self, iid: str, args: dict[str, Any], tool_call_id: str) -> ToolOutcome:
        action, act_args, why = str(args.get("action", "")), dict(args.get("args") or {}), str(args.get("rationale", ""))
        spec = ACTIONS.get(action)
        if spec is None:
            return ToolOutcome(text=f"ERROR: unknown action {action!r}", ok=False)
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            policy = self.policy(s)
            item = next((p for p in inc.plan if _same_action(p, action, act_args) and p.get("status") in ("approved", "failed")), None)
            high = spec["risk"] == "high" and policy.get("high_risk_always_human", True)
            needs_person = (item is None and self.mode(s, inc, "remediate") == "human") or (high and not (item and item.get("approved_by") not in (None, "autopilot")))
            if needs_person:
                appr = Approval(incident_id=iid, session_id=None, server_id=inc.server_id, kind="action", stage=inc.stage,
                                title=f"{spec['title']}: {_args_text(act_args)}", summary=why, risk=spec["risk"],
                                payload={"action": action, "args": act_args, "rationale": why, "tool_call_id": tool_call_id,
                                         "in_plan": item is not None, "reason": "high-risk action" if high else "not in the approved plan"})
                s.add(appr)
                s.flush()
                inc.state = "waiting"
                inc.waiting_reason = appr.title
                add_event(s, inc, "approval", f"Waiting for a person: {appr.title}", why, data={"approval_id": appr.id, "risk": spec["risk"], "action": action, "args": act_args})
                audit(s, "raiden", f"action.{action}", inc.server_id or "", "approval", actor_type="raiden", incident=iid, args=act_args)
                approval_id = appr.id
        if needs_person:
            self.rt.notify("approval", incident_id=iid, approval_id=approval_id)
            return ToolOutcome(text="waiting for approval", pause={"approval_id": approval_id})
        return ToolOutcome(text=await self._do_action(iid, action, act_args, why, "autopilot" if not item else (item.get("approved_by") or "autopilot")))

    async def _do_action(self, iid: str, action: str, args: dict[str, Any], why: str, approved_by: str) -> str:
        with self.rt.db.session() as s:
            server_id = s.get(Incident, iid).server_id
        t0 = time.monotonic()
        try:
            res = await self.rt.driver(server_id).execute_action(action, args)
            ok, message = True, res.get("message", "done")
        except DriverError as exc:
            ok, message = False, str(exc)
        ms = int((time.monotonic() - t0) * 1000)
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            plan = [dict(p) for p in inc.plan]
            for p in plan:
                if _same_action(p, action, args) and p.get("status") in ("approved", "failed", "proposed"):
                    p["status"] = "done" if ok else "failed"
                    p["result"] = message
                    break
            inc.plan = plan
            add_event(s, inc, "action", f"{ACTIONS[action]['title']}: {_args_text(args)}", message, stage=inc.stage,
                      data={"action": action, "args": args, "ok": ok, "ms": ms, "approved_by": approved_by, "risk": ACTIONS[action]["risk"], "rationale": why})
            audit(s, "raiden", f"action.{action}", server_id or "", "allow" if ok else "error", actor_type="raiden", incident=iid, args=args,
                  approved_by=approved_by, result=message[:300])
        self.rt.bus.publish("incident", id=iid)
        return (f"OK: {message}" if ok else f"FAILED: {message}") + f" (approved by {approved_by})"

    async def _decision_text(self, iid: str, approval_id: int) -> str | None:
        with self.rt.db.session() as s:
            appr = s.get(Approval, approval_id)
            if appr is None or appr.status == "pending":
                return None
            status, by, comment, payload = appr.status, appr.decided_by or "a person", appr.comment, dict(appr.payload)
        if status == "approved":
            return "APPROVED. " + await self._do_action(iid, payload["action"], payload["args"], payload.get("rationale", ""), by)
        return f"REJECTED by {by}" + (f": {comment}" if comment else "") + ". Do not retry this action; choose another route or escalate."

    # ================================================================================================ outcomes
    async def _handle(self, iid: str, stage: str, step: str, result: LoopResult) -> bool:
        """Apply a session result. True = keep advancing in this worker."""
        if result.status == "paused":
            with self.rt.db.session() as s:
                inc = s.get(Incident, iid)
                if inc.state == "running":
                    inc.state = "waiting"
            return False
        if result.status == "no_endpoint":
            with self.rt.db.session() as s:
                inc = s.get(Incident, iid)
                inc.state, inc.assignee = "waiting", "on-call"
                inc.waiting_reason = f"No AI endpoint available: {result.error}"
                add_event(s, inc, "escalated", "Waiting for a person: no AI endpoint available", result.error, actor="system")
            self.rt.notify("escalated", incident_id=iid)
            return False
        if result.status != "done" or not result.done:
            self._escalate(iid, f"rAIden could not finish {stage}: {result.error or 'it stopped without a conclusion'}")
            return False
        tool, args = result.done["tool"], result.done["args"]
        if tool == "escalate":
            self._escalate(iid, str(args.get("reason", "")))
            return False
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            if tool == "submit_triage":
                inc.severity = args.get("severity", inc.severity)
                inc.category = args.get("category", inc.category)
                inc.summary = args.get("summary", inc.summary)
                inc.hypothesis = args.get("hypothesis", "")
                add_event(s, inc, "triage", f"Triage: {inc.severity.upper()} · {fmt.category(inc.category)}", f"{inc.summary}\n\n**Hypothesis:** {inc.hypothesis}",
                          data={"severity": inc.severity, "category": inc.category})
                inc.state = "queued"
                return self._gate(s, inc, "diagnose", material=inc.summary)
            if tool == "submit_diagnosis":
                inc.root_cause = args.get("root_cause", "")
                inc.plan = _plan_items(args.get("plan") or [])
                ev = "\n".join(f"- {e}" for e in args.get("evidence") or [])
                add_event(s, inc, "diagnosis", "Root cause found" if args.get("confidence") != "low" else "Probable root cause",
                          f"{inc.root_cause}\n\n**Evidence**\n{ev}\n\n**Plan**\n{plan_text(inc.plan)}",
                          data={"confidence": args.get("confidence"), "plan": inc.plan, "evidence": args.get("evidence") or []})
                if not inc.plan:
                    inc.state = "running"
                    s.commit()
                    self._escalate(iid, "No safe action fits this root cause: a person has to decide.")
                    return False
                inc.state = "queued"
                too_long = len(inc.plan) > int(self.policy(s).get("max_auto_actions", 8))
                return self._gate(s, inc, "remediate", material=plan_text(inc.plan), force_human=too_long)
            if tool == "submit_remediation":
                add_event(s, inc, "remediation", "Remediation done", args.get("summary", ""))
                set_stage(s, inc, "verify")
                window = self.policy(s)["verify_window_s"] / (self.rt.settings.demo_speed if self.rt.settings.demo else 1)
                inc.verify_until = utcnow() + timedelta(seconds=window)
                inc.state, inc.step = "idle", ""
                inc.waiting_reason = f"Watching the metrics for {fmt.duration(window)}"
                return False
            if tool == "submit_verification":
                passed = bool(args.get("passed"))
                ev = "\n".join(f"- {e}" for e in args.get("evidence") or [])
                add_event(s, inc, "verification", "Verified: the fix holds" if passed else "Verification failed", f"{args.get('summary', '')}\n\n{ev}",
                          data={"passed": passed})
                if passed:
                    inc.state, inc.step = "queued", ""
                    return self._gate(s, inc, "verify", material=args.get("summary", ""))
                return self._recur(s, inc, f"Verification failed: {args.get('summary', '')}")
            if tool == "propose_hardening":
                items = []
                for i, it in enumerate(args.get("items") or []):
                    risk = "low"
                    if it.get("kind") == "action":
                        risk = ACTIONS.get((it.get("spec") or {}).get("action", ""), {}).get("risk", "high")
                    items.append({"id": i + 1, "kind": it.get("kind"), "title": it.get("title", ""), "detail": it.get("detail", ""),
                                  "spec": it.get("spec") or {}, "risk": risk, "status": "proposed"})
                inc.hardening = items
                add_event(s, inc, "hardening", f"Proposed {fmt.plural(len(items), 'protection')}", args.get("summary", "") + "\n\n" +
                          "\n".join(f"- **{h['kind']}**: {h['title']}" for h in items), data={"items": items})
                inc.state = "queued"
                if not items:
                    inc.step = "report"
                    return True
                return self._gate(s, inc, "harden", material="\n".join(f"- {h['title']}" for h in items))
            if tool == "submit_report":
                inc.report = args.get("report_markdown", "")
                add_event(s, inc, "report", "Incident report written", "")
                inc.state = "queued"
                return self._gate(s, inc, "resolved", material="")
        return False

    def _recur(self, s: Any, inc: Incident, why: str) -> bool:
        inc.attempts += 1
        if inc.attempts >= 3:
            inc.state = "running"
            s.commit()
            self._escalate(inc.id, f"The problem came back {inc.attempts} times: {why}")
            return False
        set_stage(s, inc, "diagnose", actor="system", note=why)
        inc.step, inc.state, inc.verify_until = "", "queued", None
        sess = self._session(s, inc)
        sess.messages = list(sess.messages) + [{"role": "user", "content": f"{why}\nGo back to DIAGNOSE: look again (the first fix did not hold), then submit_diagnosis with a new plan."}]
        sess.cursor = "diagnose/"
        return True

    def recurrence(self, iid: str, why: str) -> None:
        """The incident's own signal fired again during verification."""
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None or inc.stage != "verify":
                return
            add_event(s, inc, "recurrence", "The signal fired again during verification", why, actor="system")
            self._recur(s, inc, f"Recurrence: {why}")
        self.rt.bus.publish("incident", id=iid)

    def _escalate(self, iid: str, reason: str) -> None:
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None:
                return
            inc.state, inc.assignee, inc.waiting_reason = "escalated", "on-call", reason
            add_event(s, inc, "escalated", "Escalated to a person", reason)
            audit(s, "raiden", "incident.escalate", iid, actor_type="raiden", reason=reason)
        self.rt.notify("escalated", incident_id=iid)

    # ================================================================================================ gates
    def _gate(self, s: Any, inc: Incident, gate: str, material: str, force_human: bool = False) -> bool:
        """True when the gate opened right away (autopilot, or a learned approval)."""
        mode = "human" if force_human else self.mode(s, inc, gate)
        if mode == "auto":
            add_event(s, inc, "autopilot", f"Autopilot: {STAGE_INFO[gate]['auto']}", actor="autopilot", data={"gate": gate})
            self._open_gate(s, inc, gate, "autopilot")
            return True
        if gate == "remediate" and not force_human:
            learned = learning.covering(s, inc.plan, inc.category, inc.server_id, learning.environment_of(s, inc.server_id), self.policy(s))
            if learned:
                self._learned_gate(s, inc, learned, material)
                return True
        payload: dict[str, Any] = {"gate": gate}
        risk = "low"
        if gate == "remediate":
            payload["plan"] = inc.plan
            payload["root_cause"] = inc.root_cause
            risk = max((p["risk"] for p in inc.plan), key=lambda r: ["low", "medium", "high"].index(r), default="low")
        if gate == "harden":
            payload["items"] = inc.hardening
            risk = max((h["risk"] for h in inc.hardening), key=lambda r: ["low", "medium", "high"].index(r), default="low")
        appr = Approval(incident_id=inc.id, server_id=inc.server_id, kind="gate", stage=gate, title=GATE_TITLES[gate], summary=material,
                        payload=payload, risk=risk)
        s.add(appr)
        s.flush()
        inc.state, inc.waiting_reason = "waiting", GATE_TITLES[gate]
        add_event(s, inc, "approval", f"Waiting for a person: {GATE_TITLES[gate].lower()}", STAGE_INFO[gate]["human"],
                  data={"approval_id": appr.id, "gate": gate, "risk": risk}, actor="system")
        self.rt.notify("approval", incident_id=inc.id, approval_id=appr.id)
        return False

    def _learned_gate(self, s: Any, inc: Incident, learned: list[LearnedApproval], material: str) -> None:
        """People approved this kind of plan every time and accepted not to be asked again: the gate opens on its own, with
        the decision on record like any other."""
        actor = "learned approval " + ", ".join(f"#{la.id}" for la in learned)
        emails = {p for la in learned for p in la.approvers or []} | {la.decided_by for la in learned if la.decided_by}
        names = {u.email: u.name or u.email for u in s.execute(select(User).where(User.email.in_(emails))).scalars()}
        who = sorted({names.get(p, p) for la in learned for p in la.approvers or []})
        accepted = sorted({names.get(la.decided_by, la.decided_by) for la in learned if la.decided_by})
        times = max(la.approvals for la in learned)
        why = (f"Not asked again: approved {times} times in a row" + (f" by {', '.join(who)}" if who else "") +
               (f"; {', '.join(accepted)} accepted not to be asked" if accepted else "") + ".")
        risk = max((p["risk"] for p in inc.plan), key=lambda r: ["low", "medium", "high"].index(r), default="low")
        s.add(Approval(incident_id=inc.id, server_id=inc.server_id, kind="gate", stage="remediate", title=GATE_TITLES["remediate"], summary=material,
                       payload={"gate": "remediate", "plan": inc.plan, "root_cause": inc.root_cause, "learned": [la.id for la in learned]}, risk=risk,
                       status="approved", decided_at=utcnow(), decided_by=actor, comment=why))
        learning.use(learned)
        add_event(s, inc, "learned", f"Learned approval: {'; '.join(learning.describe_approval(la) for la in learned)}", why, actor=actor,
                  data={"learned": [la.id for la in learned], "gate": "remediate"})
        audit(s, "raiden", "approval.learned", inc.id, "allow", actor_type="raiden", learned=[la.id for la in learned],
              plan=[p.get("action") for p in inc.plan])
        self._open_gate(s, inc, "remediate", actor)
        self.rt.bus.publish("knowledge")

    def _open_gate(self, s: Any, inc: Incident, gate: str, actor: str) -> None:
        if gate in ("triage", "diagnose"):
            set_stage(s, inc, gate, actor=actor)
            inc.state, inc.step = "queued", ""
        elif gate == "remediate":
            inc.plan = [{**p, "status": "approved", "approved_by": actor} if p.get("status") == "proposed" else p for p in inc.plan]
            ev = dict(inc.evidence or {})
            ev["_plan_approved_by"] = actor
            inc.evidence = ev
            set_stage(s, inc, "remediate", actor=actor)
            inc.state, inc.step = "queued", ""
        elif gate == "verify":
            set_stage(s, inc, "harden", actor=actor)
            inc.state, inc.step = "queued", ""
        elif gate == "harden":
            inc.hardening = [{**h, "status": "approved", "approved_by": actor} if h.get("status") == "proposed" else h for h in inc.hardening]
            inc.state, inc.step = "queued", "apply"
        elif gate == "resolved":
            set_stage(s, inc, "resolved", actor=actor)
            inc.state, inc.step = "done", ""
            inc.waiting_reason = ""
            if not inc.summary:
                inc.summary = inc.root_cause
        inc.waiting_reason = "" if inc.state != "waiting" else inc.waiting_reason

    # ================================================================================================ people
    def decide(self, approval_id: int, approve: bool, user: str, comment: str = "") -> dict[str, Any]:
        with self.rt.db.session() as s:
            appr = s.get(Approval, approval_id)
            if appr is None:
                raise KeyError("approval not found")
            if appr.status != "pending":
                raise ValueError(f"already {appr.status}")
            appr.status = "approved" if approve else "rejected"
            appr.decided_at, appr.decided_by, appr.comment = utcnow(), user, comment
            audit(s, user, f"approval.{'approve' if approve else 'reject'}", appr.incident_id or appr.conversation_id or "", "allow" if approve else "deny",
                  title=appr.title, comment=comment)
            if appr.incident_id:
                inc = s.get(Incident, appr.incident_id)
                add_event(s, inc, "decision", decision_title(approve, appr.title), comment, actor=user, data={"approval_id": appr.id, "approved": approve})
                if appr.kind == "action":
                    inc.state = "queued"  # the worker executes (or reports the rejection) and resumes the session
                elif approve:
                    self._open_gate(s, inc, appr.stage, user)
                else:
                    self._reject_gate(s, inc, appr.stage, user, comment)
                learned = self._learn(s, appr, inc, approve, user, comment)
            else:
                learned = False
            result = {"id": appr.id, "status": appr.status, "incident_id": appr.incident_id, "conversation_id": appr.conversation_id}
        if result["incident_id"]:
            self.rt.bus.publish("incident", id=result["incident_id"])
        if learned:
            self.rt.bus.publish("knowledge")
        self.rt.bus.publish("approval", id=approval_id)
        return result

    def _learn(self, s: Any, appr: Approval, inc: Incident, approve: bool, user: str, comment: str) -> bool:
        """A decision on a plan teaches rAIden: approving it again and again makes a proposal not to ask again (a person
        accepts it); rejecting an action revokes what was learned for it here."""
        if not (appr.kind == "action" or (appr.kind == "gate" and appr.stage == "remediate")):
            return False
        pl = appr.payload or {}
        actions = [str(p.get("action")) for p in pl.get("plan") or []] if appr.kind == "gate" else [str(pl.get("action"))]
        env = learning.environment_of(s, inc.server_id)
        if not approve:
            gone = learning.revoke_for(s, actions, inc.category, inc.server_id, env, user, comment)
            for la in gone:
                add_event(s, inc, "learned", f"rAIden asks again: {learning.describe_approval(la)}", f"{user} rejected it" + (f": {comment}" if comment else "."),
                          actor=user, data={"learned": [la.id], "revoked": True})
                audit(s, user, "learned_approval.revoke", f"learned approval #{la.id}", "allow", reason=comment or "rejected")
            return bool(gone)
        policy = self.policy(s)
        if appr.kind != "gate" or not policy.get("learn_approvals", True):
            return False
        s.flush()  # this decision counts
        found = [c for c in learning.approval_patterns(s, int(policy.get("learn_after", 3)))
                 if c["action"] in actions and c["category"] == inc.category and c["environment"] == env]
        for la in learning.propose_approvals(s, found, by="raiden"):
            add_event(s, inc, "learned", f"rAIden proposes not to ask again: {learning.describe_approval(la)}",
                      f"{la.reason} A person accepts or declines it on the Knowledge page.", data={"learned": [la.id], "proposed": True})
        return bool(found)

    def _reject_gate(self, s: Any, inc: Incident, gate: str, user: str, comment: str) -> None:
        note = f"{user} rejected" + (f": {comment}" if comment else "")
        if gate == "triage":
            set_stage(s, inc, "resolved", actor=user, note=f"Dismissed: {comment or 'not an incident'}")
            inc.summary = inc.summary or f"Dismissed by {user}"
            inc.state = "done"
        elif gate == "remediate":
            sess = self._session(s, inc)
            sess.messages = list(sess.messages) + [{"role": "user", "content": f"The plan was rejected by {user}" + (f": {comment}" if comment else "") +
                                                   ". Propose a different plan with submit_diagnosis, or escalate."}]
            sess.cursor = "diagnose/"
            inc.plan = [{**p, "status": "rejected"} for p in inc.plan]
            inc.state = "queued"
        elif gate == "harden":
            inc.hardening = [{**h, "status": "rejected"} for h in inc.hardening]
            inc.state, inc.step = "queued", "report"
        elif gate == "verify":
            self._recur(s, inc, f"{note}: the fix does not hold")
        elif gate == "resolved":
            sess = self._session(s, inc)
            sess.messages = list(sess.messages) + [{"role": "user", "content": f"{note}. Revise the report and call submit_report again."}]
            sess.cursor = "harden/report"
            inc.state, inc.step = "queued", "report"
        else:
            inc.state, inc.assignee, inc.waiting_reason = "escalated", user, note

    def take_over(self, iid: str, user: str) -> None:
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            inc.state, inc.assignee, inc.waiting_reason = "escalated", user, f"{user} took over"
            for appr in s.execute(select(Approval).where(Approval.incident_id == iid, Approval.status == "pending")).scalars():
                appr.status, appr.decided_by, appr.decided_at = "cancelled", user, utcnow()
            add_event(s, inc, "takeover", f"{user} took over the incident", actor=user)
            audit(s, user, "incident.take_over", iid)
        self.rt.bus.publish("incident", id=iid)

    def hand_back(self, iid: str, user: str, note: str = "") -> None:
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            if inc.stage == "verify" and inc.verify_until:
                inc.state = "idle"
            else:
                inc.state = "queued"
            inc.assignee, inc.waiting_reason = "raiden", ""
            sess = s.execute(select(AgentSession).where(AgentSession.incident_id == iid)).scalar_one_or_none()
            if sess and note:
                sess.messages = list(sess.messages) + [{"role": "user", "content": f"{user} handed the incident back to you: {note}"}]
            self.retry_at.pop(iid, None)
            add_event(s, inc, "handback", f"{user} handed the incident back to rAIden", note, actor=user)
            audit(s, user, "incident.hand_back", iid)
        self.rt.bus.publish("incident", id=iid)

    def move(self, iid: str, stage: str, user: str) -> None:
        """Drag and drop on the board: a person moves the card. Forward past a gate counts as approving it."""
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            if inc is None or stage == inc.stage:
                return
            for appr in s.execute(select(Approval).where(Approval.incident_id == iid, Approval.status == "pending")).scalars():
                appr.status, appr.decided_by, appr.decided_at, appr.comment = "cancelled", user, utcnow(), f"card moved to {stage}"
            set_stage(s, inc, stage, actor=user, note="moved on the board")
            if stage == "resolved":
                inc.state = "done"
                inc.summary = inc.summary or f"Resolved by {user}"
            elif stage == "verify":
                inc.verify_until = utcnow() + timedelta(seconds=60)
                inc.state = "idle"
            else:
                inc.state, inc.step = "queued", ""
                if stage == "remediate":
                    inc.plan = [{**p, "status": "approved", "approved_by": user} if p.get("status") == "proposed" else p for p in inc.plan]
                sess = s.execute(select(AgentSession).where(AgentSession.incident_id == iid)).scalar_one_or_none()
                if sess is not None:
                    sess.cursor = ""
            audit(s, user, "incident.move", iid, stage=stage)
        self.rt.bus.publish("incident", id=iid)

    # ================================================================================================ hardening
    async def _apply_hardening(self, iid: str) -> None:
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            items = [dict(h) for h in inc.hardening]
            server_id, category = inc.server_id, inc.category
            policy = self.policy(s)
        for h in items:
            if h.get("status") != "approved":
                continue
            spec = h.get("spec") or {}
            by = h.get("approved_by", "autopilot")
            try:
                if h["kind"] == "monitor":
                    with self.rt.db.session() as s:
                        mon = Monitor(name=h["title"][:200], description=h.get("detail", ""), metric=spec["metric"], aggregation=spec.get("aggregation", "avg"),
                                      operator=spec.get("operator", ">"), threshold=float(spec["threshold"]), window_minutes=int(spec.get("window_minutes", 5)),
                                      severity=spec.get("severity", "sev3"), category=category, scope={"servers": [server_id]} if server_id else {},
                                      action=spec.get("action", "investigate"), created_by=f"raiden ({iid})")
                        s.add(mon)
                    h["status"], h["result"] = "applied", "monitor created"
                elif h["kind"] == "action":
                    risk = ACTIONS.get(spec.get("action", ""), {}).get("risk", "high")
                    if risk == "high" and by == "autopilot" and policy.get("high_risk_always_human", True):
                        h["status"], h["result"] = "skipped", "high-risk: needs a person"
                    else:
                        res = await self.rt.driver(server_id).execute_action(spec["action"], spec.get("args") or {})
                        h["status"], h["result"] = "applied", res.get("message", "done")
                elif h["kind"] == "routine":
                    with self.rt.db.session() as s:
                        h["status"], h["result"] = "applied", self.rt.enable_routine(s, spec.get("template", ""), server_id)
                elif h["kind"] == "recommendation":
                    # a change only a person can make: it stays on the server's suggestions until somebody does it or dismisses it
                    if server_id:
                        with self.rt.db.session() as s:
                            upsert_suggestion(s, server_id, kind="suggestion", category=category or "configuration", title=h["title"][:300],
                                              detail=(h.get("detail") or "") + f"\n\n**How:**\n```\n{spec.get('how', '')}\n```\n\nFrom incident {iid}.",
                                              source=f"incident {iid}", fingerprint=f"incident:{iid}:{h['id']}", data={"incident": iid, "how": spec.get("how", "")})
                    h["status"], h["result"] = "suggested", "on the server's suggestions for a person" if server_id else "in the incident report for a person"
                elif h["kind"] == "lesson":  # a fact about this server from now on (the Knowledge page shows where it came from)
                    with self.rt.db.session() as s:
                        k, _ = learning.remember(s, kind="fact", title=str(spec.get("text") or h["title"]), body=str(h.get("detail") or ""),
                                                 scope={"server": server_id} if server_id else {}, source="incident", author=iid,
                                                 evidence=[{"type": "incident", "id": iid, "by": by, "text": h["title"]}])
                        kid = k.id
                    h["status"], h["result"] = "applied", f"remembered as knowledge #{kid}"
                    self.rt.bus.publish("knowledge")
                else:
                    h["status"], h["result"] = "failed", f"unknown kind {h['kind']!r}"
            except (DriverError, KeyError, ValueError, TypeError) as exc:
                h["status"], h["result"] = "failed", str(exc)[:300]
        with self.rt.db.session() as s:
            inc = s.get(Incident, iid)
            inc.hardening = items
            applied = [h for h in items if h.get("status") == "applied"]
            handed = [h for h in items if h.get("status") == "suggested"]
            failed = [h for h in items if h.get("status") == "failed"]
            title = ", ".join([f"{fmt.plural(len(applied), 'protection')} added"] + ([f"{len(handed)} left to a person"] if handed else [])
                              + ([f"{len(failed)} failed"] if failed else []))
            add_event(s, inc, "hardened", title, "\n".join(f"- {h['title']}: {h.get('result', '')}" for h in items), data={"items": items})
            for h in applied + handed:
                audit(s, "raiden", f"harden.{h['kind']}", server_id or "", "allow", actor_type="raiden", incident=iid, title=h["title"], approved_by=h.get("approved_by"))
            inc.step, inc.state = "report", "queued"


MANAGED = {"azure_flexible", "aws_rds", "aws_aurora", "gcp_cloudsql"}


def monitor_spec_problems(spec: dict[str, Any]) -> list[str]:
    """The rules of the Monitors API (MonitorIn), for a monitor the model proposes."""
    out = []
    if spec.get("metric") not in BY_ID:
        out.append(f"unknown metric {spec.get('metric')!r}, use a catalog metric id")
    if spec.get("operator", ">") not in (">", ">=", "<", "<="):
        out.append(f"operator {spec.get('operator')!r} must be one of >, >=, <, <=")
    if spec.get("aggregation", "avg") not in ("avg", "max", "min", "last"):
        out.append(f"aggregation {spec.get('aggregation')!r} must be avg, max, min or last")
    if spec.get("severity", "sev3") not in ("sev1", "sev2", "sev3", "sev4"):
        out.append(f"severity {spec.get('severity')!r} must be sev1, sev2, sev3 or sev4")
    try:
        float(spec["threshold"])
        if not 1 <= int(spec.get("window_minutes", 5)) <= 1440:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        out.append("threshold must be a number and window_minutes 1 to 1440")
    return out


def cannot(action: str, caps: dict[str, Any], platform: str) -> str:
    """Why rAIden's login cannot carry out this action on this server ('' when it can, or when nothing is known yet)."""
    if action in ("alter_system_set", "reload_config") and platform in MANAGED:
        return f"{action} is not available on {platform}: server parameters are set with the provider"
    need = ACTION_NEEDS.get(action)
    if need and caps and not caps.get("superuser") and not caps.get(need):
        return f"rAIden's login lacks {need} on this server, so {action} would fail"
    return ""


def _args_text(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in args.items()) if args else "no arguments"


TOOL_TITLES = {
    "current_metrics": "Read the current metrics", "metric_history": "Looked at {metric} over {minutes} min", "server_overview": "Server overview",
    "list_sessions": "Listed sessions", "lock_tree": "Checked who blocks whom", "top_queries": "Top queries by {order_by}", "table_health": "Table health",
    "wraparound_status": "Transaction ID age and xmin holders", "replication_status": "Replication status", "get_settings": "Read settings",
    "run_sql": "Ran a read-only query", "recent_incidents": "Recalled earlier incidents", "list_servers": "Listed the fleet", "fleet_metric": "{metric} across the fleet",
}


def _tool_title(name: str, args: dict[str, Any]) -> str:
    t = TOOL_TITLES.get(name, name)
    try:
        return t.format(**{"metric": args.get("metric", ""), "minutes": args.get("minutes", 60), "order_by": (args.get("order_by") or "total_time").replace("_", " ")})
    except (KeyError, IndexError):
        return t


def _trim_preview(p: Any) -> Any:
    if isinstance(p, dict) and "rows" in p:
        out = {k: v for k, v in p.items() if k != "rows"}
        out["rows"] = p["rows"][:25]
        if len(p["rows"]) > 25:
            out["rows_omitted"] = len(p["rows"]) - 25
        return out
    return p
