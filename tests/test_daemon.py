import asyncio
import json
import time

from raiden.daemon import Daemon
from raiden.incident import Incident, classify, parse_jsonlog_line
from tests.fakes import FakeLLM, call


def make_incident(kind="crash", critical=True, sig=None):
    sig = sig or crash_event(0).signature
    return Incident(
        id="inc-20261020T050000-test", opened_ts=0.0, opened_iso="2026-10-20T05:00:00.000Z", reason="critical event: crash x3",
        kinds={kind: 3}, signatures={sig: {"count": 3, "kind": kind, "first": "a", "last": "b", "sample": "boom"}}, events_total=3,
        sample=["a line"], critical=critical,
    )


def crash_event(ts):
    ev = classify(parse_jsonlog_line(json.dumps({"timestamp": "2026-10-20 05:00:00.000 UTC", "error_severity": "LOG", "message": "server process (PID 77) was terminated by signal 9: Killed"})))
    ev.ts = ts
    return ev


def types(cfg):
    return [json.loads(x)["type"] for x in cfg.events_path.read_text().splitlines()]


GOOD = [
    [call("bash", command="echo diagnosing")],
    [call("write_file", path="$RAIDEN_INCIDENT_DIR/report.md", content="# report")],
    [call("finish", resolution="resolved", summary="fixed")],
]


def test_incident_end_to_end(cfg):
    d = Daemon(cfg)
    d.llm = FakeLLM(GOOD)
    inc = make_incident()
    res = asyncio.run(d.handle(inc))
    assert res.resolution == "resolved"
    meta = json.loads((cfg.incidents_dir / inc.id / "incident.json").read_text())
    assert meta["outcome"] == "resolved" and meta["num_turns"] == 3 and meta["usage"]["prompt_tokens"] == 300
    assert (cfg.incidents_dir / inc.id / "incident.md").read_text().startswith("Incident inc-")
    t = types(cfg)
    assert t[:2] == ["incident_opened", "incident_dispatched"] and "incident_closed" in t
    assert t[-2:] == ["incident_verified_quiet", "incident_settled"]
    assert not d.busy


def test_first_prompt_contains_the_incident_and_the_rules(cfg):
    d = Daemon(cfg)
    d.llm = FakeLLM(GOOD)
    asyncio.run(d.handle(make_incident()))
    system, user = d.llm.seen[0][0]["content"], d.llm.seen[0][1]["content"]
    assert "Prime directives" in system and "## Skill index" in system
    assert "INCIDENT inc-20261020T050000-test" in user and "boom" in user


def test_a_failed_model_call_never_leaves_the_daemon_stuck(cfg):
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("down")

    d = Daemon(cfg)
    d.llm = Boom()
    res = asyncio.run(d.handle(make_incident()))
    assert res.resolution is None and not d.busy
    meta = json.loads((cfg.incidents_dir / "inc-20261020T050000-test" / "incident.json").read_text())
    assert meta["outcome"] == "error" and "down" in meta["error"]
    assert "incident_verified_quiet" not in types(cfg) and types(cfg)[-1] == "incident_settled"


def test_recurrence_after_a_fix_is_detected(cfg):
    async def scenario():
        d = Daemon(cfg)
        d.llm = FakeLLM(GOOD)
        consumer = asyncio.create_task(d._consume())
        task = asyncio.create_task(d.handle(make_incident()))
        for _ in range(100):
            if d.verifying:
                break
            await asyncio.sleep(0.05)
        d.queue.put_nowait(crash_event(time.time() + 0.5))
        await task
        consumer.cancel()
        return d

    d = asyncio.run(scenario())
    assert "incident_recurred" in types(cfg) and "incident_verified_quiet" not in types(cfg)
    assert d.recurrence_hint == "inc-20261020T050000-test"
    inc = d.agg.poll(recurrence_of=d.recurrence_hint) if d.agg.has_open_window() else None
    assert d.agg.has_open_window() or inc is not None  # the recurring crash is queued for the next incident


def test_echoes_of_the_handled_incident_do_not_cause_a_second_one(cfg):
    async def scenario():
        d = Daemon(cfg)
        d.llm = FakeLLM(GOOD)
        consumer = asyncio.create_task(d._consume())
        await d.handle(make_incident())
        stale_crash = crash_event(d.finish_epoch - 0.2)  # same signature, logged before the agent finished
        variant = classify(parse_jsonlog_line(json.dumps({"timestamp": "x", "error_severity": "LOG", "message": "checkpointer process (PID 5) was terminated by signal 15: Terminated"})))
        variant.ts = d.finish_epoch - 3.0  # same kind, different signature: still covered by the finished incident
        noise = classify(parse_jsonlog_line(json.dumps({"timestamp": "x", "error_severity": "FATAL", "state_code": "57P01", "message": "terminating connection due to administrator command"})))
        noise.ts = d.finish_epoch - 0.5
        for e in (stale_crash, variant, noise):
            d.queue.put_nowait(e)
        await asyncio.sleep(0.3)
        consumer.cancel()
        return d

    d = asyncio.run(scenario())
    assert not d.agg.has_open_window()


def test_a_new_kind_after_the_fix_is_not_swallowed(cfg):
    async def scenario():
        d = Daemon(cfg)
        d.llm = FakeLLM(GOOD)
        consumer = asyncio.create_task(d._consume())
        await d.handle(make_incident())
        corruption = classify(parse_jsonlog_line(json.dumps({"timestamp": "x", "error_severity": "ERROR", "state_code": "XX001", "message": "unexpected chunk number 2 (expected 1) for toast value 5 in pg_toast_9"})))
        corruption.ts = time.time() + 0.1
        d.queue.put_nowait(corruption)
        await asyncio.sleep(0.3)
        consumer.cancel()
        return d

    assert asyncio.run(scenario()).agg.has_open_window()


def test_evolve_runs_after_a_quiet_critical_incident_and_artifacts_are_validated(cfg):
    import dataclasses

    cfg2 = dataclasses.replace(cfg, evolve_enabled=True)
    skill = cfg2.skills_dir / "learned-demo" / "SKILL.md"
    bad = cfg2.skills_dir / "learned-bad" / "SKILL.md"
    check = cfg2.checks_dir / "90-demo.sh"
    d = Daemon(cfg2)
    d.llm = FakeLLM(GOOD + [
        [call("write_file", path=str(skill), content="---\nname: learned-demo\ndescription: demo runbook\n---\nsteps")],
        [call("write_file", path=str(bad), content="no frontmatter here")],
        [call("write_file", path=str(check), content="#!/bin/bash\necho 'OK demo: fine'\n")],
        [call("finish", resolution="resolved", summary="evolved")],
    ])
    asyncio.run(d.handle(make_incident()))
    t = types(cfg2)
    assert t.index("incident_verified_quiet") < t.index("evolution_started") < t.index("evolution_done")
    done = [json.loads(x) for x in cfg2.events_path.read_text().splitlines() if '"evolution_done"' in x][0]
    actions = {c["path"].split("/")[-2] + "/" + c["path"].split("/")[-1]: c["action"] for c in done["changes"]}
    assert actions["learned-demo/SKILL.md"] == "added" and actions["learned-bad/SKILL.md"] == "quarantined" and actions["checks.d/90-demo.sh"] == "added"
    assert skill.exists() and not bad.exists() and check.exists()
    assert list(cfg2.quarantine_dir.rglob("SKILL.md"))


def test_no_evolve_when_the_fix_did_not_hold(cfg):
    import dataclasses

    cfg2 = dataclasses.replace(cfg, evolve_enabled=True)

    async def scenario():
        d = Daemon(cfg2)
        d.llm = FakeLLM(GOOD)
        consumer = asyncio.create_task(d._consume())
        task = asyncio.create_task(d.handle(make_incident()))
        for _ in range(100):
            if d.verifying:
                break
            await asyncio.sleep(0.05)
        d.queue.put_nowait(crash_event(time.time() + 0.5))
        await task
        consumer.cancel()

    asyncio.run(scenario())
    assert "evolution_started" not in types(cfg2)


def test_patrol_conditions_seen_by_an_incident_are_acknowledged(cfg):
    import os

    script = cfg.checks_dir / "80-crash.sh"
    script.write_text("#!/bin/bash\necho 'CRIT crash-recovery: 3 crash recoveries in the last 30 min'\nexit 2\n")
    os.chmod(script, 0o755)
    d = Daemon(cfg)
    d.llm = FakeLLM(GOOD)
    asyncio.run(d.handle(make_incident()))
    until = d._patrol_until["patrol:crash-recovery"]
    assert until - time.monotonic() > 1700  # silenced for about half an hour, not forever


def test_a_config_reload_in_the_log_runs_the_patrol_right_away(cfg, monkeypatch):
    import dataclasses

    runs = []
    ok = [{"script": "10-x.sh", "status": "OK", "name": "x", "message": "fine"}]
    monkeypatch.setattr("raiden.daemon.run_checks", lambda c, *a, **k: runs.append(1) or ok)
    real_sleep = asyncio.sleep

    async def fast_sleep(delay, *a, **k):
        await real_sleep(0.01)

    monkeypatch.setattr("raiden.daemon.asyncio.sleep", fast_sleep)
    c = dataclasses.replace(cfg, patrol_interval_s=3600.0, patrol_min_gap_s=0.0)
    d = Daemon(c)
    reload_line = classify(parse_jsonlog_line(json.dumps({"timestamp": "2026-10-20 05:00:00.000 UTC", "error_severity": "LOG", "message": "received SIGHUP, reloading configuration files"})))

    async def scenario():
        tasks = [asyncio.create_task(d._patrol_loop()), asyncio.create_task(d._consume())]
        await real_sleep(0.3)
        assert len(runs) == 1  # the scheduled first run; the next one is an hour away
        await d.queue.put(reload_line)
        await real_sleep(0.3)
        for t in tasks:
            t.cancel()
        return len(runs)

    assert asyncio.run(scenario()) == 2  # ... and the reload made it run now
    assert "patrol_triggered" in types(c)
    assert not d.agg.has_open_window()  # a reload is never an incident by itself


def test_a_failing_patrol_check_after_a_reload_wakes_the_agent(cfg, monkeypatch):
    import dataclasses

    bad = [{"script": "55-x.sh", "status": "CRIT", "name": "planned-preload", "message": "planned shared_preload_libraries includes pg_crash"}]
    monkeypatch.setattr("raiden.daemon.run_checks", lambda c, *a, **k: bad)
    handled = []

    async def fake_handle(self, inc):
        handled.append(inc)
        self.busy = False

    monkeypatch.setattr(Daemon, "handle", fake_handle)
    d = Daemon(dataclasses.replace(cfg, patrol_interval_s=3600.0))
    asyncio.run(d._patrol_once())
    assert len(handled) == 1 and handled[0].critical and "planned-preload" in handled[0].reason
    assert "patrol:planned-preload" in handled[0].signature_set


def _log_event(message, sev="LOG", state=""):
    return classify(parse_jsonlog_line(json.dumps({"timestamp": "2026-10-20 05:00:00.000 UTC", "error_severity": sev, "state_code": state, "message": message})))


def test_clients_of_a_planned_shutdown_do_not_open_an_incident(cfg):
    d = Daemon(cfg)
    noise = [_log_event("received fast shutdown request")]
    noise += [_log_event("terminating connection due to administrator command", "FATAL", "57P01") for _ in range(6)]
    noise += [_log_event("the database system is shutting down", "FATAL", "57P03") for _ in range(6)]
    noise += [_log_event('background worker "logical replication launcher" (PID 77) exited with exit code 1')]

    async def scenario():
        task = asyncio.create_task(d._consume())
        for e in noise:
            await d.queue.put(e)
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(scenario())
    assert not d.agg.has_open_window()


def test_the_same_disconnects_without_a_shutdown_request_still_wake_the_agent(cfg):
    d = Daemon(cfg)
    noise = [_log_event("terminating connection due to administrator command", "FATAL", "57P01") for _ in range(4)]

    async def scenario():
        task = asyncio.create_task(d._consume())
        for e in noise:
            await d.queue.put(e)
        await asyncio.sleep(0.3)
        task.cancel()

    asyncio.run(scenario())
    assert d.agg.has_open_window()
