"""rAIden learns from the people it works with (in-process, SQLite, no PostgreSQL server; the scripted demo model plays the
AI where one is needed): what people teach goes into the prompts where it applies; approving the same plan again and
again leads to not being asked any more, once a person accepts; a rejection takes that back; a dream turns people's
words into instructions and fixed incidents into playbooks, and can be undone."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raiden.console import learning
from raiden.console.app import create_app
from raiden.console.models import AIEndpoint, Approval, AuditEntry, ChatMessage, Conversation, Incident, IncidentEvent, Knowledge, LearnedApproval
from raiden.console.pipeline import _plan_items
from raiden.console.services import add_event, create_incident, put_setting
from raiden.console.settings import ConsoleSettings

H = {"X-Requested-With": "raiden"}
ADA = {"email": "ada@example.com", "password": "correct horse battery", "name": "Ada Admin"}
OLI = {"email": "oli@example.com", "password": "operator password 1", "name": "Oli Operator", "role": "operator"}
ARGS = {"terminate_backend": {"pid": 4242}, "terminate_sessions": {"role": "reporting", "state": "idle"}, "cancel_backend": {"pid": 77},
        "set_role_timeout": {"role": "migrator", "setting": "lock_timeout", "value": "3s"}, "execute_sql": {"sql": "VACUUM FULL orders.orders"}}


@pytest.fixture()
def client(tmp_path: Path):
    settings = ConsoleSettings.from_env(data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'console.db'}", demo=False, secret_key="")
    with TestClient(create_app(settings, start_background=False)) as c:
        assert c.post("/api/v1/auth/setup", json=ADA).status_code == 200
        assert c.post("/api/v1/users", json=OLI, headers=H).status_code == 200
        for sid, env in (("orders-prod", "production"), ("inventory-prod", "production"), ("analytics-staging", "staging")):
            r = c.post("/api/v1/servers", json={"id": sid, "name": sid, "environment": env, "platform": "self_managed", "host": "10.0.0.5", "port": 5432,
                                                "dbname": "postgres", "username": "raiden_monitor", "password": "s3cret-monitor"}, headers=H)
            assert r.status_code == 200, r.text
        yield c


def as_user(c: TestClient, who: dict) -> None:
    c.cookies.clear()
    assert c.post("/api/v1/auth/login", json={"email": who["email"], "password": who["password"]}).status_code == 200


def diagnosed(rt, sid: str, category: str, actions: list[str]) -> tuple[str, bool, int | None]:
    """An incident whose plan arrives at the remediation gate: (incident, opened on its own, the approval)."""
    with rt.db.session() as s:
        inc = create_incident(s, server_id=sid, title=f"{category} on {sid}", severity="sev2", category=category, source="manual")
        inc.stage, inc.state = "diagnose", "queued"
        inc.plan = _plan_items([{"action": a, "args": ARGS[a], "rationale": "test"} for a in actions])
        opened = rt.pipeline._gate(s, inc, "remediate", material="the plan")
        appr = s.execute(select(Approval).where(Approval.incident_id == inc.id).order_by(Approval.id.desc())).scalars().first()
        return inc.id, opened, appr.id if appr else None


def resolved(rt, sid: str, category: str, action: str, root: str) -> str:
    with rt.db.session() as s:
        inc = create_incident(s, server_id=sid, title=f"{category} on {sid}", severity="sev2", category=category, source="manual")
        inc.plan = [{**p, "status": "done", "approved_by": ADA["email"]} for p in _plan_items([{"action": action, "args": ARGS[action], "rationale": "x"}])]
        inc.hardening = [{"id": 1, "kind": "monitor", "title": "Transaction open > 15 min", "status": "applied"}]
        inc.stage, inc.state, inc.root_cause = "resolved", "done", root
        from raiden.console.db import utcnow

        inc.resolved_at = utcnow()
        return inc.id


def scripted_model(rt) -> None:
    """The demo model answers for an endpoint of the purposes the dream and the chat use."""
    from raiden.console.demo.model import DemoProvider

    rt.router.demo_provider = DemoProvider(speed=200).as_model
    with rt.db.session() as s:
        s.add(AIEndpoint(name="scripted", provider="demo", model="raiden-demo", purposes=["routines", "chat"], enabled=True, simulated=True))


def wait_dream(c: TestClient, after: int = 0) -> dict:
    end = time.monotonic() + 30
    while time.monotonic() < end:
        dreams = c.get("/api/v1/knowledge").json()["dreams"]
        if dreams and dreams[0]["id"] > after and dreams[0]["status"] != "running":
            return dreams[0]
        time.sleep(0.2)
    raise AssertionError("the dream did not finish")


# ============================================================================================================ taught by people
def test_what_people_teach_goes_where_it_applies(client: TestClient):
    rt = client.app.state.rt
    teach = [
        {"kind": "rule", "title": "Never end sessions of the replication role"},
        {"kind": "fact", "title": "The migrator role deploys at 14:00 UTC on weekdays", "scope": {"server": "orders-prod"}},
        {"kind": "rule", "title": "End the root blocker, never the waiting DDL", "scope": {"environment": "production", "category": "locks"}},
        {"kind": "preference", "title": "Put the exact SQL in every recommendation", "retention": "permanent"},
    ]
    ids = [client.post("/api/v1/knowledge", json=t, headers=H).json()["id"] for t in teach]
    assert client.post("/api/v1/knowledge", json=teach[0], headers=H).status_code == 409  # taught twice is one instruction
    assert client.post("/api/v1/knowledge", json={"kind": "fact", "title": "x on a server nobody has", "scope": {"server": "nope"}}, headers=H).status_code == 400
    assert client.post("/api/v1/knowledge", json={"kind": "skill", "title": "A playbook without a kind of incident"}, headers=H).status_code == 400

    with rt.db.session() as s:
        orders = learning.prompt_block(s, "orders-prod", "production")
        staging = learning.prompt_block(s, "analytics-staging", "staging")
        locks = learning.prompt_block(s, "orders-prod", "production", "locks", only_category=True)
    assert f"#{ids[0]}" in orders and f"#{ids[1]}" in orders and f"#{ids[3]}" in orders and f"#{ids[2]}" not in orders  # the locks rule waits for triage
    assert f"#{ids[1]}" not in staging and f"#{ids[2]}" not in staging and f"#{ids[0]}" in staging
    assert f"#{ids[2]}" in locks and f"#{ids[0]}" not in locks
    assert "taught by ada@example.com" in orders

    # into the incident: the session context, and the diagnosis once triage named the kind of incident
    iid, _, _ = diagnosed(rt, "orders-prod", "locks", ["terminate_backend"])
    with rt.db.session() as s:
        inc = s.get(Incident, iid)
        context = rt.pipeline._session(s, inc).messages[1]["content"]
        diagnose = rt.pipeline._instructions(s, inc, "diagnose", "")
    assert "The migrator role deploys at 14:00 UTC" in context and "End the root blocker" not in context
    assert "End the root blocker, never the waiting DDL" in diagnose

    # retired: out of every prompt, the version before kept
    r = client.patch(f"/api/v1/knowledge/{ids[1]}", json={"status": "retired", "why": "the deploy window moved"}, headers=H)
    assert r.status_code == 200 and r.json()["history"][-1]["status"] == "active" and r.json()["history"][-1]["why"] == "the deploy window moved"
    with rt.db.session() as s:
        assert "14:00" not in learning.prompt_block(s, "orders-prod", "production")
        assert s.get(Knowledge, ids[0]).uses >= 3

    as_user(client, OLI)  # operators teach and edit; deleting is an admin's
    assert client.post("/api/v1/knowledge", json={"kind": "fact", "title": "crm-prod has no read replica"}, headers=H).status_code == 200
    assert client.delete(f"/api/v1/knowledge/{ids[3]}", headers=H).status_code == 403


def test_lessons_of_an_older_console_become_facts(client: TestClient):
    rt = client.app.state.rt
    with rt.db.session() as s:
        put_setting(s, "lessons", {"orders-prod": [{"text": "Metabase opens a connection per tile", "incident": "INC-1004"}],
                                   "_fleet": [{"text": "Check pg_file_settings after a crash loop", "incident": "INC-1009"}], "gone": [{"text": "x"}]})
    with rt.db.session() as s:
        assert learning.migrate_lessons(s) == 2
    with rt.db.session() as s:
        facts = {k.title: k for k in s.execute(select(Knowledge)).scalars()}
        assert facts["Metabase opens a connection per tile"].scope == {"server": "orders-prod"} and facts["Metabase opens a connection per tile"].author == "INC-1004"
        assert facts["Check pg_file_settings after a crash loop"].scope == {}
        assert learning.migrate_lessons(s) == 0  # once


# ============================================================================================================ not asking again
def test_the_same_plan_approved_three_times_is_not_asked_again(client: TestClient):
    rt = client.app.state.rt
    # orders-prod and inventory-prod, both production: lock queues, the same plan each time
    for i, sid in enumerate(("orders-prod", "inventory-prod", "orders-prod")):
        iid, opened, aid = diagnosed(rt, sid, "locks", ["terminate_backend"])
        assert not opened and aid
        if i == 2:  # before the third decision the plan shows how often it was approved
            hint = next(a for a in client.get(f"/api/v1/incidents/{iid}").json()["pending_approvals"])["learning"]
            assert hint["actions"][0]["approved"] == 2 and hint["learn_after"] == 3 and hint["actions"][0]["learned"] is None
        rt.pipeline.decide(aid, True, ADA["email"] if i != 1 else OLI["email"], "")
        with rt.db.session() as s:
            assert len(s.execute(select(LearnedApproval)).scalars().all()) == (1 if i == 2 else 0)
    events = client.get(f"/api/v1/incidents/{iid}").json()["events"]
    assert any(e["kind"] == "learned" and e["title"].startswith("rAIden proposes not to ask again: Terminate a session") for e in events)
    la = client.get("/api/v1/knowledge").json()["learned_approvals"][0]
    assert (la["status"], la["action"], la["category"], la["environment"], la["server_id"], la["approvals"]) == \
        ("proposed", "terminate_backend", "locks", "production", None, 3)
    assert la["approvers"] == sorted([ADA["email"], OLI["email"]])

    # a proposal changes nothing yet; accepting it is an admin's call
    _, opened, _ = diagnosed(rt, "orders-prod", "locks", ["terminate_backend"])
    assert not opened
    as_user(client, OLI)
    assert client.post(f"/api/v1/learned-approvals/{la['id']}/accept", json={}, headers=H).status_code == 403
    as_user(client, ADA)
    assert client.post(f"/api/v1/learned-approvals/{la['id']}/accept", json={"reason": "every time the same"}, headers=H).json()["status"] == "active"

    # the next lock queue in production: the gate opens on its own, on the record
    iid, opened, aid = diagnosed(rt, "inventory-prod", "locks", ["terminate_backend"])
    assert opened
    detail = client.get(f"/api/v1/incidents/{iid}").json()
    assert detail["stage"] == "remediate" and detail["plan"][0]["approved_by"] == f"learned approval #{la['id']}"
    decision = next(a for a in detail["approvals"] if a["id"] == aid)
    assert decision["status"] == "approved" and decision["decided_by"] == f"learned approval #{la['id']}" and "approved 3 times in a row" in decision["comment"]
    assert any(e["kind"] == "learned" and e["title"].startswith("Learned approval: Terminate a session") for e in detail["events"])
    with rt.db.session() as s:
        assert s.get(LearnedApproval, la["id"]).uses == 1
        assert s.execute(select(AuditEntry).where(AuditEntry.action == "approval.learned")).scalars().first() is not None
        assert s.execute(select(AuditEntry).where(AuditEntry.action == "learned_approval.accept")).scalars().first().actor == ADA["email"]

    # never beyond what was learned: another kind of incident, a high-risk step, staging is the policy's anyway
    assert not diagnosed(rt, "orders-prod", "connections", ["terminate_backend"])[1]
    assert not diagnosed(rt, "orders-prod", "locks", ["terminate_backend", "execute_sql"])[1]
    assert not diagnosed(rt, "orders-prod", "locks", ["terminate_backend", "set_role_timeout"])[1]

    # the kill switch: with learning off, rAIden asks every time
    assert client.put("/api/v1/autonomy", json={"learn_approvals": False}, headers=H).json()["policy"]["learn_approvals"] is False
    assert not diagnosed(rt, "orders-prod", "locks", ["terminate_backend"])[1]
    client.put("/api/v1/autonomy", json={"learn_approvals": True}, headers=H)

    # a rejection is a correction: rAIden asks again, and the count starts over
    iid, opened, aid = diagnosed(rt, "orders-prod", "locks", ["terminate_backend", "set_role_timeout"])
    rt.pipeline.decide(aid, False, OLI["email"], "not during the month-end close")
    with rt.db.session() as s:
        gone = s.get(LearnedApproval, la["id"])
        assert gone.status == "revoked" and gone.decided_by == OLI["email"] and gone.reason == "not during the month-end close"
        assert learning.approval_patterns(s, 3) == []
    assert any(e["kind"] == "learned" and e["title"].startswith("rAIden asks again") for e in client.get(f"/api/v1/incidents/{iid}").json()["events"])
    assert not diagnosed(rt, "inventory-prod", "locks", ["terminate_backend"])[1]


def test_high_risk_actions_are_never_learned(client: TestClient):
    rt = client.app.state.rt
    for _ in range(4):
        _, _, aid = diagnosed(rt, "orders-prod", "replication", ["execute_sql"])
        rt.pipeline.decide(aid, True, ADA["email"], "")
    with rt.db.session() as s:
        assert learning.approval_patterns(s, 3) == []
        la = LearnedApproval(action="execute_sql", category="replication", environment="production", status="proposed")
        s.add(la)
        s.flush()
        lid = la.id
    assert client.post(f"/api/v1/learned-approvals/{lid}/accept", json={}, headers=H).status_code == 400


# ============================================================================================================ dreams
def test_a_dream_learns_from_words_writes_playbooks_and_can_be_undone(client: TestClient):
    rt = client.app.state.rt
    scripted_model(rt)
    # what happened during the day
    first = resolved(rt, "orders-prod", "locks", "terminate_backend", "An analyst's transaction blocked a migration's ALTER TABLE.")
    resolved(rt, "inventory-prod", "locks", "terminate_backend", "A forgotten transaction held a lock the sync needed.")
    with rt.db.session() as s:
        add_event(s, s.get(Incident, first), "comment", "Ada commented",
                  "The analyst role is for ad-hoc reports: always end its sessions first when they block a deploy.", actor=ADA["email"])
        conv = Conversation(id="c1", title="deploys", server_id="orders-prod", user_email=ADA["email"])
        s.add(conv)
        s.flush()
        s.add(ChatMessage(conversation_id="c1", role="user", content="remember that orders-prod deploys run at 14:00 UTC", data={"user": ADA["email"]}))
        s.add(ChatMessage(conversation_id="c1", role="user", content="what is blocking orders-prod right now?", data={"user": ADA["email"]}))
    _, _, aid = diagnosed(rt, "orders-prod", "replication", ["terminate_backend"])
    rt.pipeline.decide(aid, False, OLI["email"], "Never end sessions of the replication role: the standby falls behind.")
    _, _, aid = diagnosed(rt, "inventory-prod", "connections", ["terminate_sessions"])
    rt.pipeline.decide(aid, True, ADA["email"], "Fine. No need to ask me for this next time.")

    assert client.post("/api/v1/dreams", headers=H).status_code == 200
    d = wait_dream(client)
    assert d["status"] == "done" and d["tokens"] > 0 and d["summary"].startswith("Learned")
    assert d["read"] == {"decisions": 2, "notes": 1, "statements": 2, "incidents": 2, "dismissed": 0}
    ops = {(c["op"], c["kind"]) for c in d["changes"]}
    assert ops == {("add", "fact"), ("add", "rule"), ("add", "skill"), ("propose", "learned approval")}
    page = client.get("/api/v1/knowledge").json()
    items = {k["title"]: k for k in page["items"]}
    fact = items["Orders-prod deploys run at 14:00 UTC."]
    assert fact["status"] == "active" and fact["scope"] == {"server": "orders-prod"} and fact["evidence"][0]["type"] == "chat"
    rule = items["Never end sessions of the replication role: the standby falls behind."]
    assert rule["status"] == "active" and rule["scope"] == {"server": "orders-prod", "category": "replication"} and rule["source"] == "dream"
    assert items["The analyst role is for ad-hoc reports: always end its sessions first when they block a deploy."]["scope"] == \
        {"server": "orders-prod", "category": "locks"}
    skill = next(k for k in page["items"] if k["kind"] == "skill")
    assert skill["scope"] == {"category": "locks"} and "terminate_backend" in skill["body"] and "Transaction open > 15 min" in skill["body"]
    proposal = page["learned_approvals"][0]
    assert (proposal["action"], proposal["category"], proposal["status"]) == ("terminate_sessions", "connections", "proposed")
    assert "No need to ask me" in proposal["evidence"][0]["comment"]
    runs = [r for r in client.get("/api/v1/routines").json() if r["key"] == "dream"][0]["recent_runs"]
    assert runs[0]["status"] == "ok" and runs[0]["summary"] == d["summary"]
    # the playbook goes with the diagnosis of the next lock queue
    iid, _, _ = diagnosed(rt, "inventory-prod", "locks", ["terminate_backend"])
    with rt.db.session() as s:
        assert f"playbook #{skill['id']}" in rt.pipeline._instructions(s, s.get(Incident, iid), "diagnose", "")

    # nothing new: no model call
    assert client.post("/api/v1/dreams", headers=H).status_code == 200
    quiet = wait_dream(client, d["id"])
    assert quiet["status"] == "done" and quiet["tokens"] == 0 and quiet["summary"].startswith("Nothing new to read")

    # undo: everything the dream did goes back, except what a person changed since
    client.patch(f"/api/v1/knowledge/{fact['id']}", json={"title": "orders-prod deploys run at 14:00 and 20:00 UTC"}, headers=H)
    as_user(client, OLI)
    assert client.post(f"/api/v1/dreams/{d['id']}/undo", headers=H).status_code == 403
    as_user(client, ADA)
    r = client.post(f"/api/v1/dreams/{d['id']}/undo", headers=H).json()
    assert r["status"] == "undone" and r["kept"] == ["Orders-prod deploys run at 14:00 UTC."]
    page = client.get("/api/v1/knowledge").json()
    status = {k["id"]: k["status"] for k in page["items"]}
    assert status[fact["id"]] == "active" and status[rule["id"]] == "retired" and status[skill["id"]] == "retired"
    assert page["learned_approvals"] == [] and page["dreams"][1]["status"] == "undone"
    assert client.post(f"/api/v1/dreams/{d['id']}/undo", headers=H).status_code == 409


def test_a_dream_cites_its_evidence_and_leaves_people_s_words_alone(client: TestClient):
    """The checks behind the dream's tools, whatever model dreams: no evidence, no instruction; inferred is a proposal;
    what a person taught changes only on a person's words; high-risk actions are never proposed."""
    rt = client.app.state.rt
    taught = client.post("/api/v1/knowledge", json={"kind": "rule", "title": "Never touch payments_app sessions"}, headers=H).json()
    _, _, aid = diagnosed(rt, "orders-prod", "locks", ["terminate_backend"])
    rt.pipeline.decide(aid, True, ADA["email"], "")
    with rt.db.session() as s:
        m, _ = learning.material(s, {})
    idx = learning.evidence_index(m)
    dream_id = 99
    changes: list = []
    with rt.db.session() as s:
        tool = lambda name, args: rt.dreamer._tool(s, dream_id, name, args, m, idx, changes, {})  # noqa: E731
        with pytest.raises(ValueError, match="cite the evidence"):
            tool("add_instruction", {"kind": "rule", "title": "Always do X", "evidence": []})
        with pytest.raises(ValueError, match="not in the material"):
            tool("add_instruction", {"kind": "rule", "title": "Always do X", "evidence": [{"type": "note", "id": "12345"}]})
        out = tool("add_instruction", {"kind": "fact", "title": "Ada approves lock fixes on orders-prod", "evidence": [{"type": "decision", "id": str(aid)}]})
        assert "as a proposal" in out  # a decision without words: inferred, so a person confirms it
        with pytest.raises(ValueError, match="only a person's own words"):
            tool("retire_instruction", {"id": taught["id"], "why": "seems unused", "evidence": [{"type": "decision", "id": str(aid)}]})
        with pytest.raises(ValueError, match="high-risk"):
            tool("propose_learned_approval", {"action": "execute_sql", "category": "locks", "environment": "production", "why": "x",
                                              "evidence": [{"type": "decision", "id": str(aid)}]})
        with pytest.raises(ValueError, match="skill_candidates"):
            tool("write_skill", {"category": "locks", "title": "t", "body": "b", "evidence": [{"type": "decision", "id": str(aid)}]})
    assert [c["status"] for c in changes] == ["proposed"]


def test_chat_remembers_what_a_person_says(client: TestClient):
    rt = client.app.state.rt
    scripted_model(rt)
    cid = client.post("/api/v1/chat/conversations", json={"server_id": "orders-prod"}, headers=H).json()["id"]
    body = client.post(f"/api/v1/chat/conversations/{cid}/messages", json={"content": "Never end sessions of the backup role on orders-prod"}, headers=H).text
    assert "event: tool" in body and "Remembered for **orders-prod**" in body
    with rt.db.session() as s:
        k = s.execute(select(Knowledge)).scalars().one()
        said = s.execute(select(ChatMessage).where(ChatMessage.conversation_id == cid, ChatMessage.role == "user")).scalars().one()
        assert (k.kind, k.source, k.author, k.scope, k.status) == ("rule", "person", ADA["email"], {"server": "orders-prod"}, "active")
        assert k.evidence[0]["id"] == str(said.id)
        # the next conversation about orders-prod starts with it
        conv = Conversation(id="c2", title="next", server_id="orders-prod", user_email=ADA["email"])
        s.add(conv)
        s.flush()
        assert f"#{k.id}" in rt.chat._session(s, conv, ADA["email"]).messages[0]["content"]
        assert s.execute(select(IncidentEvent)).first() is None
