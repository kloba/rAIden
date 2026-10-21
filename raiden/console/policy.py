"""Autonomy policy: for every stage of an incident, does rAIden carry on by itself or does a person approve first?

The policy is set per environment (production, staging, development) and can be overridden per server. A gate sits
BEFORE its stage: 'human' on Remediate means rAIden diagnoses on its own, then waits with its plan until somebody approves.
"""

from __future__ import annotations

from typing import Any

STAGES = ["detected", "triage", "diagnose", "remediate", "verify", "harden", "resolved"]
GATED = ["triage", "diagnose", "remediate", "verify", "harden", "resolved"]
ENVIRONMENTS = ["production", "staging", "development"]
MODES = ("auto", "human")

STAGE_INFO: dict[str, dict[str, str]] = {
    "detected": {"title": "Detected", "verb": "detect", "auto": "", "human": ""},
    "triage": {"title": "Triage", "verb": "triage",
               "auto": "rAIden starts looking as soon as something is detected.",
               "human": "A person acknowledges the incident before rAIden starts."},
    "diagnose": {"title": "Diagnose", "verb": "diagnose",
                 "auto": "rAIden investigates the root cause with read-only tools.",
                 "human": "A person confirms the triage before rAIden investigates."},
    "remediate": {"title": "Remediate", "verb": "remediate",
                  "auto": "rAIden carries out its plan on its own.",
                  "human": "rAIden proposes a plan; a person approves it before anything changes."},
    "verify": {"title": "Verify", "verb": "verify",
               "auto": "rAIden checks the fix against the metrics.",
               "human": "A person confirms the fix holds."},
    "harden": {"title": "Harden", "verb": "harden",
               "auto": "rAIden adds the protections it proposes (monitors, limits, timeouts).",
               "human": "rAIden proposes protections; a person approves them."},
    "resolved": {"title": "Resolved", "verb": "resolve",
                 "auto": "rAIden closes the incident with its report.",
                 "human": "A person reviews the report and closes the incident."},
}

DEFAULT_POLICY: dict[str, Any] = {
    "environments": {
        "production": {"triage": "auto", "diagnose": "auto", "remediate": "human", "verify": "auto", "harden": "human", "resolved": "auto"},
        "staging": {"triage": "auto", "diagnose": "auto", "remediate": "auto", "verify": "auto", "harden": "human", "resolved": "auto"},
        "development": {"triage": "auto", "diagnose": "auto", "remediate": "auto", "verify": "auto", "harden": "auto", "resolved": "auto"},
    },
    "high_risk_always_human": True,      # drop a slot, roll back a prepared transaction, free SQL: always a person
    "budget_exhausted": "human",         # no AI budget left: every gate waits for a person
    "verify_window_s": 180,
    "max_auto_actions": 8,               # a plan longer than this always needs a person
}


def merged(policy: dict[str, Any] | None) -> dict[str, Any]:
    out = {**DEFAULT_POLICY, **(policy or {})}
    envs = {e: dict(DEFAULT_POLICY["environments"][e]) for e in ENVIRONMENTS}
    for env, stages in ((policy or {}).get("environments") or {}).items():
        if env in envs and isinstance(stages, dict):
            envs[env].update({s: m for s, m in stages.items() if s in GATED and m in MODES})
    out["environments"] = envs
    return out


def mode_for(policy: dict[str, Any], environment: str, server_overrides: dict[str, Any] | None, stage: str) -> str:
    if stage not in GATED:
        return "auto"
    override = (server_overrides or {}).get(stage)
    if override in MODES:
        return override
    env = policy["environments"].get(environment) or policy["environments"]["production"]
    return env.get(stage, "human")


def describe(policy: dict[str, Any]) -> dict[str, Any]:
    return {"stages": [{"id": s, **STAGE_INFO[s]} for s in STAGES], "environments": ENVIRONMENTS, "policy": policy}
