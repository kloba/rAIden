"""The console's HTTP API end to end, in-process (FastAPI TestClient, SQLite, no PostgreSQL server and no AI endpoint
needed): first-run setup, sessions and roles, the CSRF header, servers, the host agent's heartbeat and incidents, the
autonomy policy, monitors, routines, AI endpoints, incidents and the audit trail."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from raiden.console.app import create_app
from raiden.console.settings import ConsoleSettings

H = {"X-Requested-With": "raiden"}
ADMIN = {"email": "ada@example.com", "password": "correct horse battery", "name": "Ada Admin"}


@pytest.fixture()
def client(tmp_path: Path):
    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'console.db'}", demo=False, secret_key="")
    app = create_app(settings, start_background=False)
    with TestClient(app) as c:
        yield c


def setup_admin(c: TestClient) -> None:
    assert c.get("/api/v1/auth/status").json()["needs_setup"] is True
    r = c.post("/api/v1/auth/setup", json=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"


def make_server(c: TestClient, sid: str = "pg-orders", **over) -> dict:
    body = {"id": sid, "name": sid, "environment": "production", "platform": "self_managed", "host": "10.0.0.5", "port": 5432,
            "dbname": "postgres", "username": "raiden_monitor", "password": "s3cret-monitor", "host_info": {"ram_gb": 64, "vcpus": 16}, **over}
    r = c.post("/api/v1/servers", json=body, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_setup_once_then_sign_in(client: TestClient):
    setup_admin(client)
    assert client.post("/api/v1/auth/setup", json=ADMIN).status_code == 409  # only the very first account
    me = client.get("/api/v1/auth/me").json()
    assert me["email"] == ADMIN["email"] and "password" not in str(me)
    client.post("/api/v1/auth/logout")
    client.cookies.clear()
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": ADMIN["email"], "password": "wrong password!"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "ADA@example.com", "password": ADMIN["password"]}).status_code == 200


def test_weak_password_refused(client: TestClient):
    r = client.post("/api/v1/auth/setup", json={**ADMIN, "password": "1234567890"})
    assert r.status_code == 400


def test_changes_need_the_csrf_header(client: TestClient):
    setup_admin(client)
    assert client.post("/api/v1/servers", json={"name": "x", "host": "h"}).status_code == 403
    assert client.get("/api/v1/servers").status_code == 200  # reads do not


def test_roles(client: TestClient):
    setup_admin(client)
    r = client.post("/api/v1/users", json={"email": "vic@example.com", "name": "Vic Viewer", "role": "viewer", "password": "viewer password 1"}, headers=H)
    assert r.status_code == 200
    client.cookies.clear()
    client.post("/api/v1/auth/login", json={"email": "vic@example.com", "password": "viewer password 1"})
    assert client.get("/api/v1/estate").status_code == 200
    assert client.post("/api/v1/servers", json={"name": "x", "host": "h"}, headers=H).status_code == 403
    assert client.put("/api/v1/autonomy", json={"verify_window_s": 30}, headers=H).status_code == 403
    assert client.post("/api/v1/incidents", json={"title": "something is slow"}, headers=H).status_code == 403
    assert client.get("/api/v1/users").status_code == 403


def test_servers_keep_secrets_and_validate_input(client: TestClient):
    setup_admin(client)
    srv = make_server(client)
    assert srv["has_password"] is True and "s3cret" not in str(srv)
    assert srv["host_info"] == {"ram_gb": 64.0, "vcpus": 16}
    assert client.post("/api/v1/servers", json={"id": "pg-orders", "name": "dup", "host": "h"}, headers=H).status_code == 409
    assert client.post("/api/v1/servers", json={"id": "Bad Id!", "name": "x", "host": "h"}, headers=H).status_code == 400
    assert client.post("/api/v1/servers", json={"name": "x", "host": "h", "platform": "mainframe"}, headers=H).status_code == 400
    r = client.patch("/api/v1/servers/pg-orders", json={"environment": "staging", "tags": {"app": "orders"}}, headers=H)
    assert r.json()["environment"] == "staging" and r.json()["tags"] == {"app": "orders"} and r.json()["has_password"] is True
    rows = client.get("/api/v1/estate").json()
    assert rows[0]["id"] == "pg-orders" and rows[0]["supervised_stages"] == ["harden"]  # staging defaults
    actions = {a["id"]: a for a in client.get("/api/v1/metrics/catalog").json()["actions"]}
    assert actions["drop_replication_slot"]["risk"] == "high"


def test_host_agent_heartbeat_and_incidents(client: TestClient):
    setup_admin(client)
    make_server(client)
    tok = client.post("/api/v1/servers/pg-orders/agent-token", headers=H).json()
    assert tok["token"].startswith("rdn_") and "RAIDEN_SERVER_ID=pg-orders" in tok["env"]
    agent = {"Authorization": f"Bearer {tok['token']}"}
    assert client.post("/api/v1/agents/heartbeat", json={"server_id": "pg-orders", "metrics": {}}, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/v1/agents/heartbeat", json={"server_id": "pg-orders", "metrics": {"cpu_percent": 42.0, "bogus": 1}, "agent": {"version": "0.1.0"}}, headers=agent)
    assert r.status_code == 200
    srv = client.get("/api/v1/servers/pg-orders").json()
    assert srv["host_agent"] is True and srv["agent_last_seen_at"]
    events = [
        {"type": "incident_opened", "id": "20261021-0001", "reason": "PANIC: could not write to file", "critical": True, "kinds": ["panic"]},
        {"type": "agent_tool", "incident": "20261021-0001", "tool": "bash", "summary": "df -h /var/lib/postgresql", "denied": False},
        {"type": "guard_denied", "incident": "20261021-0001", "tool": "bash", "rule": "no-poweroff", "summary": "reboot"},
        {"type": "incident_closed", "id": "20261021-0001", "outcome": "resolved", "summary": "Freed 40 GiB of old WAL archives"},
    ]
    r = client.post("/api/v1/agents/events", json={"server_id": "pg-orders", "events": events}, headers=agent)
    assert r.status_code == 200 and len(r.json()["opened"]) == 1
    iid = r.json()["opened"][0]
    inc = client.get(f"/api/v1/incidents/{iid}").json()
    assert inc["source"] == "host_agent" and inc["severity"] == "sev1" and inc["stage"] == "resolved"
    kinds = [e["kind"] for e in inc["events"]]
    assert "tool" in kinds and "denied" in kinds
    # the same incident is not opened twice when the agent retries
    again = client.post("/api/v1/agents/events", json={"server_id": "pg-orders", "events": events[:1]}, headers=agent).json()
    assert again["opened"] == []


def test_autonomy_policy_and_server_override(client: TestClient):
    setup_admin(client)
    make_server(client)
    p = client.get("/api/v1/autonomy").json()
    assert p["policy"]["environments"]["production"]["remediate"] == "human"
    r = client.put("/api/v1/autonomy", json={"environments": {"production": {"remediate": "auto"}}, "verify_window_s": 30}, headers=H)
    assert r.status_code == 200 and r.json()["policy"]["environments"]["production"]["remediate"] == "auto"
    assert client.put("/api/v1/autonomy", json={"environments": {"production": {"remediate": "sometimes"}}}, headers=H).status_code == 400
    assert client.put("/api/v1/autonomy", json={"verify_window_s": 5}, headers=H).status_code == 422
    r = client.put("/api/v1/servers/pg-orders/autonomy", json={"remediate": "human"}, headers=H)
    assert r.json()["autonomy"] == {"remediate": "human"}
    assert client.get("/api/v1/autonomy").json()["overrides"][0]["id"] == "pg-orders"
    r = client.put("/api/v1/servers/pg-orders/autonomy", json={"remediate": None}, headers=H)
    assert r.json()["autonomy"] == {}
    audit = client.get("/api/v1/audit?action=autonomy").json()
    assert {a["action"] for a in audit} >= {"autonomy.update", "autonomy.server"}


def test_monitors_routines_and_endpoints(client: TestClient):
    setup_admin(client)
    monitors = client.get("/api/v1/monitors").json()
    assert len(monitors) >= 20 and all(m["builtin"] for m in monitors)
    builtin = monitors[0]
    assert client.delete(f"/api/v1/monitors/{builtin['id']}", headers=H).status_code == 400
    assert client.patch(f"/api/v1/monitors/{builtin['id']}", json={"enabled": False}, headers=H).json()["enabled"] is False
    assert client.post("/api/v1/monitors", json={"name": "Nope", "metric": "no_such_metric", "threshold": 1}, headers=H).status_code == 400
    m = client.post("/api/v1/monitors", json={"name": "Checkout CPU", "metric": "cpu_percent", "threshold": 75, "window_minutes": 5}, headers=H).json()
    assert m["builtin"] is False and client.delete(f"/api/v1/monitors/{m['id']}", headers=H).status_code == 200

    routines = client.get("/api/v1/routines").json()
    assert {r["key"] for r in routines} >= {"health-patrol", "daily-report", "index-advisor"}
    assert client.post("/api/v1/routines", json={"name": "bad", "cron": "61 * * * *"}, headers=H).status_code == 400
    r = client.post("/api/v1/routines", json={"name": "Weekday review", "kind": "agent", "prompt": "Look at the connections per role.", "cron": "0 7 * * 1-5",
                                               "timezone": "Europe/Prague"}, headers=H).json()
    assert r["schedule"] == "Weekdays at 07:00" and r["next_run_at"]

    ep = client.post("/api/v1/ai/endpoints", json={"name": "OpenAI", "provider": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-6.1",
                                                   "auth": "api_key", "api_key": "sk-test-123", "purposes": ["chat"], "monthly_cost_budget": 50}, headers=H)
    assert ep.status_code == 200 and ep.json()["has_api_key"] is True and "sk-test" not in ep.text
    assert client.post("/api/v1/ai/endpoints", json={"name": "Gossip", "provider": "openai", "model": "m", "purposes": ["gossip"]}, headers=H).status_code == 400
    routing = {r["purpose"]: r for r in client.get("/api/v1/ai/routing").json()}
    assert [e["name"] for e in routing["chat"]["endpoints"]] == ["OpenAI"] and routing["incidents"]["endpoints"] == []


def test_incidents_by_hand(client: TestClient):
    setup_admin(client)
    make_server(client)
    inc = client.post("/api/v1/incidents", json={"server_id": "pg-orders", "title": "Checkout is slow since 14:05", "severity": "sev2"}, headers=H).json()
    assert inc["stage"] == "detected" and inc["source"] == "manual"
    client.post(f"/api/v1/incidents/{inc['id']}/comment", json={"text": "Started after the 14:00 deploy"}, headers=H)
    client.post(f"/api/v1/incidents/{inc['id']}/take-over", headers=H)
    detail = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert detail["assignee"] == ADMIN["email"]
    assert any(e["kind"] == "comment" and "deploy" in e["body"] for e in detail["events"])
    assert detail["autonomy"]["remediate"] == "human" and detail["environment"] == "production"
    client.post(f"/api/v1/incidents/{inc['id']}/resolve", json={"text": "Rolled back the deploy"}, headers=H)
    assert client.get(f"/api/v1/incidents/{inc['id']}").json()["stage"] == "resolved"
    board = client.get("/api/v1/incidents?status=board").json()
    assert board[0]["id"] == inc["id"]


def test_settings_and_metrics_endpoint(client: TestClient, monkeypatch):
    setup_admin(client)
    r = client.put("/api/v1/settings/general", json={"organization": "Contoso", "retention_raw_days": 7}, headers=H)
    assert r.json()["organization"] == "Contoso"
    assert client.put("/api/v1/settings/general", json={"retention_raw_days": 500}, headers=H).status_code == 422
    s = client.get("/api/v1/settings").json()
    assert s["system"]["database"] == "SQLite" and s["system"]["demo"] is False
    assert client.get("/api/v1/prometheus").status_code == 404  # off unless RAIDEN_METRICS_TOKEN is set
    monkeypatch.setenv("RAIDEN_METRICS_TOKEN", "scrape-me")
    r = client.get("/api/v1/prometheus", headers={"Authorization": "Bearer scrape-me"})
    assert r.status_code == 200 and "raiden_pending_approvals 0" in r.text
    make_server(client, "pg-orders")
    lines = client.get("/api/v1/prometheus", headers={"Authorization": "Bearer scrape-me"}).text.splitlines()
    assert 'raiden_server_up{server="pg-orders",environment="production"} 1' in lines and 'raiden_console_info{version="0.2.0"} 1' in lines


def test_demo_onboarding_connects_to_a_simulated_untuned_server(tmp_path: Path):
    """In the demo, onboarding has a server to connect to that is not in the estate: test it, add it, assess it, apply a fix."""
    import time

    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'console.db'}", demo=True)
    with TestClient(create_app(settings, start_background=False)) as c:
        status = c.get("/api/v1/auth/status").json()
        assert c.post("/api/v1/auth/login", json=status["demo_login"]).status_code == 200
        cand = status["demo_onboarding"]
        assert cand["name"] not in {s["id"] for s in c.get("/api/v1/servers").json()}

        conn = {"host": cand["host"], "port": cand["port"], "dbname": cand["dbname"], "username": cand["username"], "password": "anything"}
        t = c.post("/api/v1/servers/test", json=conn, headers=H).json()
        checks = {x["name"]: x["ok"] for x in t["checks"]}
        assert t["ok"] and checks["pg_monitor"] and not checks["pg_stat_statements"]  # a fresh server: no query statistics yet
        host = {"ram_gb": cand["ram_gb"], "vcpus": cand["vcpus"], "storage": cand["storage"], "workload": cand["workload"]}
        srv = c.post("/api/v1/servers", json={**conn, "id": "billing", "name": "billing", "host_info": host}, headers=H).json()
        assert srv["simulated"] is True  # another id than the simulated profile's is fine

        def assessed(aid: int) -> dict:
            end = time.monotonic() + 30
            while time.monotonic() < end:
                a = c.get(f"/api/v1/assessments/{aid}").json()
                if a["status"] != "running":
                    return a
                time.sleep(0.3)
            raise AssertionError("the assessment did not finish")

        first = assessed(c.post("/api/v1/servers/billing/assess?trigger=onboarding", headers=H).json()["id"])
        assert first["status"] == "done" and first["score"] < 60 and first["summary"]
        ids = {f["id"] for f in first["findings"]}
        assert {"archiving", "password-md5", "pgss", "tune-shared_buffers", "log-log_lock_waits"} <= ids
        fix = next(f for f in first["findings"] if f["id"] == "log-log_lock_waits")["fix"]
        assert fix["kind"] == "action"
        r = c.post("/api/v1/servers/billing/actions", json={"action": fix["action"], "args": fix["args"], "reason": "onboarding"}, headers=H)
        assert r.status_code == 200, r.text
        again = assessed(c.post("/api/v1/servers/billing/assess", headers=H).json()["id"])
        assert "log-log_lock_waits" not in {f["id"] for f in again["findings"]} and again["score"] >= first["score"]


def test_resolving_an_incident_notifies_once_it_is_stored(client: TestClient):
    setup_admin(client)
    make_server(client)
    sent = []
    client.app.state.rt.notify = lambda kind, **data: sent.append((kind, data))
    inc = client.post("/api/v1/incidents", json={"server_id": "pg-orders", "title": "Replica is behind", "severity": "sev3"}, headers=H).json()
    assert not [k for k, _ in sent if k == "resolved"]
    client.post(f"/api/v1/incidents/{inc['id']}/resolve", json={"text": "Caught up after the report finished"}, headers=H)
    assert ("resolved", {"incident_id": inc["id"]}) in sent


def test_hardening_proposals_are_checked_and_person_made_changes_become_suggestions(client: TestClient):
    """From the live run: with a least-privilege login the model proposed role timeouts as 'routines' with free text, and
    they were reported as applied. Proposals are now checked while the model can still fix them, a change only a person can
    make goes on the server's suggestions, and nothing that failed is reported as applied."""
    import asyncio

    from raiden.console.agent.tools import server_brief
    from raiden.console.models import Server, Suggestion

    setup_admin(client)
    make_server(client)
    rt = client.app.state.rt
    with rt.db.session() as s:
        srv = s.get(Server, "pg-orders")
        srv.info = {**(srv.info or {}), "login": "raiden_monitor",
                    "capabilities": {"superuser": False, "pg_monitor": True, "signal_backend": True, "create_role": False, "pg_stat_statements": True}}
    with rt.db.session() as s:
        brief = server_brief(s, "pg-orders")
    assert "can cancel and end sessions" in brief and "cannot change role limits and timeouts" in brief
    inc = client.post("/api/v1/incidents", json={"server_id": "pg-orders", "title": "Lock queue", "severity": "sev2"}, headers=H).json()

    problems = rt.pipeline._hardening_problems(inc["id"], [
        {"kind": "routine", "title": "Bound migration locks", "spec": {"template": "SET lock_timeout = '2s' before DDL"}},
        {"kind": "action", "title": "lock_timeout for migrator", "spec": {"action": "set_role_timeout",
                                                                          "args": {"role": "migrator", "setting": "lock_timeout", "value": "3s"}}},
        {"kind": "recommendation", "title": "Ask the analysts", "spec": {}},
        {"kind": "monitor", "title": "Smaller queue", "spec": {"metric": "blocked_session", "threshold": 2}},
        {"kind": "monitor", "title": "Any queue", "spec": {"metric": "blocked_sessions", "threshold": 0, "severity": "warn"}},  # the live run's
    ])
    assert len(problems) == 5
    assert "unknown routine template" in problems[0] and "lacks create_role" in problems[1] and "spec.how" in problems[2] and "unknown metric" in problems[3]
    assert "severity 'warn' must be sev1" in problems[4]
    good = [
        {"kind": "monitor", "title": "Warn on a smaller lock queue", "spec": {"metric": "blocked_sessions", "operator": ">", "threshold": 2, "window_minutes": 1}},
        {"kind": "routine", "title": "Hourly wraparound watch", "spec": {"template": "wraparound-watch"}},
        {"kind": "recommendation", "title": "lock_timeout for the migrator role",
         "spec": {"how": "ALTER ROLE migrator SET lock_timeout = '3s';"}},
        {"kind": "action", "title": "End forgotten sessions", "spec": {"action": "terminate_sessions", "args": {"role": "analyst", "state": "idle in transaction"}}},
    ]
    assert rt.pipeline._hardening_problems(inc["id"], good) == []

    # applying: a routine template nobody has (it slipped through an older version) fails instead of passing as applied
    items = [{**h, "id": i + 1, "status": "approved", "approved_by": ADMIN["email"]} for i, h in enumerate(good[:3])]
    items.append({"id": 4, "kind": "routine", "title": "Made up", "spec": {"template": "no-such-routine"}, "status": "approved", "approved_by": ADMIN["email"]})
    from raiden.console.models import Incident

    with rt.db.session() as s:
        s.get(Incident, inc["id"]).hardening = items
    asyncio.run(rt.pipeline._apply_hardening(inc["id"]))
    detail = client.get(f"/api/v1/incidents/{inc['id']}").json()
    status = {h["title"]: (h["status"], h.get("result", "")) for h in detail["hardening"]}
    assert status["Warn on a smaller lock queue"][0] == "applied" and status["Hourly wraparound watch"][0] == "applied"
    assert status["lock_timeout for the migrator role"][0] == "suggested"
    assert status["Made up"] == ("failed", "no routine template 'no-such-routine'")
    hardened = next(e for e in detail["events"] if e["kind"] == "hardened")
    assert hardened["title"] == "2 protections added, 1 left to a person, 1 failed"
    with rt.db.session() as s:
        sugg = s.query(Suggestion).filter_by(server_id="pg-orders", source=f"incident {inc['id']}").one()
        assert sugg.status == "open" and "ALTER ROLE migrator SET lock_timeout" in sugg.detail
    chips = client.get("/api/v1/servers/pg-orders").json()["chips"]
    assert any(x["title"] == "lock_timeout for the migrator role" for x in chips["suggestions"])
    # the report is written with the console's own clock: the conversation has no times for stages and actions
    with rt.db.session() as s:
        text = rt.pipeline._instructions(s, s.get(Incident, inc["id"]), "harden", "report")
    assert "Left to a person (on the server's suggestions): lock_timeout for the migrator role" in text
    assert "Recorded times (UTC" in text and "2 protections added, 1 left to a person, 1 failed" in text


def test_sign_in_is_throttled_and_does_not_reveal_accounts(client: TestClient, monkeypatch):
    setup_admin(client)
    client.post("/api/v1/auth/logout")
    client.cookies.clear()
    from raiden.console.api import auth

    misses = []
    monkeypatch.setattr(auth, "verify_missing", lambda pw: misses.append(pw) or False)
    assert client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "guess one"}).status_code == 401
    assert misses == ["guess one"]  # an unknown email costs the same password check as a known one
    for i in range(4):
        assert client.post("/api/v1/auth/login", json={"email": ADMIN["email"], "password": f"wrong {i}"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": ADMIN["email"], "password": "wrong 5"})
    assert r.status_code == 401
    r = client.post("/api/v1/auth/login", json=ADMIN)  # even the right password waits now
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 800 and "Too many failed sign-ins" in r.json()["detail"]
    from raiden.console.models import AuditEntry

    denied = client.app.state.rt.db.run(lambda s: [(a.detail or {}).get("reason") for a in s.query(AuditEntry).filter_by(action="user.login", decision="deny")])
    assert len(denied) == 7 and denied[-1] == "too many failed sign-ins"  # every refusal is in the audit log


def test_responses_carry_security_headers(client: TestClient):
    r = client.get("/api/v1/auth/status")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Cache-Control"] == "no-store" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "Content-Security-Policy" not in client.get("/api/docs").headers  # Swagger UI loads its assets from a CDN
    assert "Strict-Transport-Security" not in r.headers  # only when the console is served over HTTPS


def test_an_older_console_refuses_a_newer_state_store(tmp_path: Path):
    from raiden.console.db import Database
    from raiden.console.services import put_setting

    url = f"sqlite:///{tmp_path / 'console.db'}"
    db = Database(url)
    db.create_all()
    with db.session() as s:
        put_setting(s, "schema", {"version": "9.9.9"})
    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=url, demo=False)
    with pytest.raises(RuntimeError, match="last used by rAIden 9.9.9"):
        with TestClient(create_app(settings, start_background=False)):
            pass


def test_one_console_per_state_store(tmp_path: Path):
    """A second console on the same store would collect, open incidents and act twice: it refuses to start and names the
    console that holds the store; once that one stops, the next one starts."""
    import os

    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'console.db'}", demo=False, secret_key="")
    with TestClient(create_app(settings, start_background=False)) as first:
        with pytest.raises(RuntimeError, match=rf"another rAIden Console is using this state store \(pid {os.getpid()} on .+ since 20"):
            with TestClient(create_app(settings, start_background=False)):
                pass
        assert first.get("/readyz").status_code == 200
    with TestClient(create_app(settings, start_background=False)) as second:
        assert second.get("/readyz").status_code == 200
