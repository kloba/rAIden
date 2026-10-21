"""Notifications: tell people when a decision is waiting for them, when an incident is escalated or opened (sev1/sev2),
and when it is resolved. Webhooks for Microsoft Teams, Slack or any HTTP endpoint (JSON). A webhook that fails or answers
with a server error is tried three times; one that still fails is in the audit log, where an admin will see it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx

from .models import Approval, Incident
from .services import audit, get_setting

log = logging.getLogger("raiden.console.notify")

EVENTS = {"approval": "A decision is waiting for you", "escalated": "Escalated to a person", "incident_opened": "Incident opened", "resolved": "Incident resolved"}
RETRY_S = (2.0, 8.0)  # the waits before the second and the third try


async def send(rt: Any, kind: str, data: dict[str, Any]) -> None:
    with rt.db.session() as s:
        hooks = [h for h in get_setting(s, "notifications").get("webhooks", []) if h.get("enabled", True) and kind in (h.get("events") or list(EVENTS))]
        if not hooks:
            return
        inc = s.get(Incident, data["incident_id"]) if data.get("incident_id") else None
        if kind == "incident_opened" and inc is not None and inc.severity not in ("sev1", "sev2"):
            return
        appr = s.get(Approval, data["approval_id"]) if data.get("approval_id") else None
        title = EVENTS.get(kind, kind)
        line = f"{inc.id} · {inc.severity.upper()} · {inc.title}" if inc else ""
        detail = appr.title if appr else (inc.waiting_reason if inc else "")
        link = f"{rt.settings.base_url}/incidents/{inc.id}" if inc and rt.settings.base_url else ""
        payload_generic = {"event": kind, "title": title, "incident": inc.id if inc else None, "server": inc.server_id if inc else None,
                           "severity": inc.severity if inc else None, "stage": inc.stage if inc else None, "detail": detail, "url": link}
    text = f"**{title}**\n\n{line}\n\n{detail}" + (f"\n\n[Open in rAIden]({link})" if link else "")
    async with httpx.AsyncClient(timeout=10) as client:
        for h in hooks:
            body: dict[str, Any]
            if h.get("kind") == "slack":
                body = {"text": text.replace("**", "*")}
            elif h.get("kind") == "teams":
                body = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
                    "type": "AdaptiveCard", "version": "1.4", "body": [{"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium"},
                                                                       {"type": "TextBlock", "text": line, "wrap": True}, {"type": "TextBlock", "text": detail, "wrap": True, "isSubtle": True}],
                    "actions": [{"type": "Action.OpenUrl", "title": "Open in rAIden", "url": link}] if link else []}}]}
            else:
                body = payload_generic
            error = await _deliver(client, h["url"], body)
            if error:
                name = h.get("name") or urlsplit(h["url"]).hostname or "webhook"  # not the URL: Slack's and Teams' carry a secret
                log.warning("webhook %s failed: %s", name, error)
                with rt.db.session() as s:
                    audit(s, "raiden", "notification.failed", name, "error", actor_type="raiden", event=kind, incident=payload_generic["incident"], error=error)


async def _deliver(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> str:
    """'' once delivered, otherwise what went wrong the last time. A 4xx is final: the same request will not do better."""
    error = ""
    for wait in (0.0, *RETRY_S):
        await asyncio.sleep(wait)
        try:
            r = await client.post(url, json=body)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            error = exc.__class__.__name__ + (f": {exc}" if str(exc) else "")
            continue
        if r.is_success:
            return ""
        error = f"HTTP {r.status_code}: {r.text[:160]}"
        if r.status_code < 500 and r.status_code != 429:
            break
    return error[:240]
