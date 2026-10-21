"""The whole incident loop on the demo fleet, through the HTTP API: a monitor detects a lock queue, rAIden triages and
diagnoses on its own, stops at the production gate with its plan, a person approves, rAIden fixes it, watches the
metrics, proposes protections, a person approves those, and the incident closes with a report. People had approved the
same plan three times before, so an admin accepts not to be asked again, and the next lock queue goes through the gate
on its own.

It takes about four minutes of wall time (the monitors need a minute of data), so it runs when RAIDEN_SLOW_TESTS=1.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raiden.console.app import create_app
from raiden.console.settings import ConsoleSettings

pytestmark = pytest.mark.skipif(os.environ.get("RAIDEN_SLOW_TESTS") != "1", reason="slow end-to-end test: set RAIDEN_SLOW_TESTS=1")
H = {"X-Requested-With": "raiden"}


def wait_for(fn, timeout: float, what: str):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = fn()
        if got:
            return got
        time.sleep(1.0)
    raise AssertionError(f"timed out waiting for {what}")


def test_lock_queue_from_detection_to_report(tmp_path: Path):
    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'console.db'}", demo=True, demo_speed=6.0)
    with TestClient(create_app(settings, start_background=True)) as c:
        login = c.get("/api/v1/auth/status").json()["demo_login"]
        assert c.post("/api/v1/auth/login", json=login).status_code == 200
        c.put("/api/v1/autonomy", json={"verify_window_s": 15}, headers=H)

        def gate():
            for inc in c.get("/api/v1/incidents?status=open").json():
                if inc["server_id"] == "orders-prod" and inc["category"] == "locks" and inc["pending_approvals"]:
                    return inc
            return None

        inc = wait_for(gate, 240, "the remediation gate on orders-prod")
        assert inc["stage"] in ("diagnose", "remediate") and inc["state"] == "waiting"
        approval = inc["pending_approvals"][0]
        assert approval["kind"] == "gate" and approval["payload"]["plan"][0]["action"] == "terminate_backend"
        # the plan says what rAIden learned from the decisions before: the same plan, approved three times in a row
        hint = c.get(f"/api/v1/incidents/{inc['id']}").json()["pending_approvals"][0]["learning"]["actions"][0]
        assert hint["approved"] == 3 and hint["learned"]["status"] == "proposed"
        r = c.post(f"/api/v1/approvals/{approval['id']}/decide", json={"approve": True, "comment": "go"}, headers=H)
        assert r.status_code == 200

        def hardening():
            d = c.get(f"/api/v1/incidents/{inc['id']}").json()
            pending = [a for a in d["approvals"] if a["status"] == "pending" and a["kind"] in ("hardening", "gate") and a["stage"] == "harden"]
            return (d, pending[0]) if pending else None

        detail, harden = wait_for(hardening, 180, "the protections to approve")
        assert detail["root_cause"] and any(e["kind"] == "action" for e in detail["events"])
        # orders-prod is an Azure server whose monitoring login has no CREATEROLE: role timeouts go to a person
        assert {h["kind"] for h in harden["payload"]["items"]} == {"recommendation", "monitor"}
        assert c.post(f"/api/v1/approvals/{harden['id']}/decide", json={"approve": True}, headers=H).status_code == 200

        done = wait_for(lambda: (d := c.get(f"/api/v1/incidents/{inc['id']}").json())["stage"] == "resolved" and d, 180, "the incident to resolve")
        assert done["report"] and done["mitigated_at"] and done["resolved_at"]
        assert {h["status"] for h in done["hardening"]} == {"suggested", "applied"}
        chips = c.get("/api/v1/servers/orders-prod").json()["chips"]["suggestions"]
        assert any(x["title"] == "lock_timeout = 3s for the migrator role" for x in chips)
        kinds = [e["kind"] for e in done["events"]]
        assert kinds.index("decision") < kinds.index("action") < kinds.index("verification")
        actions = [e for e in done["events"] if e["kind"] == "action"]
        assert actions[0]["data"]["approved_by"] == login["email"]
        verified = next(e for e in done["events"] if e["kind"] == "verification")
        assert "back to 0" in verified["body"]
        # the fix is real in the simulation, not only in the story: the next collection sees the queue gone
        wait_for(lambda: c.get("/api/v1/servers/orders-prod").json()["metrics"]["blocked_sessions"] < 5, 60, "the next collection to see the queue gone")

        # not asking again: an admin accepts, and the next lock queue in production goes through the gate on its own
        learned = c.post(f"/api/v1/learned-approvals/{hint['learned']['id']}/accept", json={}, headers=H).json()
        assert learned["status"] == "active" and learned["action"] == "terminate_backend"
        c.app.state.rt.fleet.begin("orders-prod", "lock_queue")

        def next_one():
            for card in c.get("/api/v1/incidents?status=board").json():
                if card["server_id"] == "orders-prod" and card["number"] > inc["number"] and card["category"] == "locks" and card["stage"] in ("remediate", "verify", "harden", "resolved"):
                    return c.get(f"/api/v1/incidents/{card['id']}").json()
            return None

        again = wait_for(next_one, 240, "the next lock queue past the remediation gate")
        assert again["plan"][0]["approved_by"] == f"learned approval #{learned['id']}"
        assert any(e["kind"] == "learned" and e["title"].startswith("Learned approval: Terminate a session") for e in again["events"])
        decided = [a for a in again["approvals"] if a["stage"] == "remediate"]
        assert [(a["status"], a["decided_by"]) for a in decided] == [("approved", f"learned approval #{learned['id']}")]
