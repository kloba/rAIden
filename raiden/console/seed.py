"""First-start defaults: the autonomy policy, general settings, built-in monitors and routines."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import monitors, routines
from .policy import DEFAULT_POLICY
from .services import get_setting, put_setting

GENERAL: dict[str, Any] = {"organization": "", "retention_raw_days": 3, "retention_rollup_days": 93, "timezone": "UTC"}


def seed_defaults(s: Session, demo: bool = False) -> None:
    if not get_setting(s, "autonomy"):
        policy = dict(DEFAULT_POLICY)
        if demo:
            policy["verify_window_s"] = 60
        put_setting(s, "autonomy", policy)
    if not get_setting(s, "general"):
        put_setting(s, "general", dict(GENERAL, retention_raw_days=2 if demo else 3, organization="Contoso" if demo else ""))
    if not get_setting(s, "notifications"):
        put_setting(s, "notifications", {"webhooks": []})
    monitors.seed_builtin(s)
    s.flush()
    routines.seed_builtin(s)
