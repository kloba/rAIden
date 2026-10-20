import json

from raiden.watcher import LogTailer


def line(message, sev="ERROR", state="XX001"):
    return json.dumps({"timestamp": "2026-10-20 05:00:00.123 UTC", "error_severity": sev, "state_code": state, "message": message, "pid": 1}) + "\n"


def test_tailer_starts_at_the_end_by_default(cfg):
    f = cfg.pg_log_dir / "raiden-20261020.json"
    f.write_text(line("unexpected chunk number 2 (expected 1) for toast value 1 in pg_toast_2"))
    t = LogTailer(cfg)
    assert t.poll_once() == []
    with open(f, "a") as fh:
        fh.write(line("missing chunk number 0 for toast value 9 in pg_toast_2"))
    evs = t.poll_once()
    assert [e.kind for e in evs] == ["corruption"]


def test_tailer_from_start_reads_history(cfg):
    f = cfg.pg_log_dir / "raiden-20261020.json"
    f.write_text(line("could not open file x") + line("checkpoint starting", "LOG", ""))
    evs = LogTailer(cfg, from_start=True).poll_once()
    assert [e.kind for e in evs] == ["corruption", ""]


def test_tailer_survives_rotation_and_partial_lines(cfg):
    a = cfg.pg_log_dir / "raiden-20261020.json"
    a.write_text("")
    t = LogTailer(cfg, from_start=True)
    t.poll_once()
    full = line("server process (PID 5) was terminated by signal 9: Killed", "LOG", "")
    with open(a, "a") as fh:
        fh.write(full[:40])  # a half-written line must not be lost or mangled
    assert t.poll_once() == []
    with open(a, "a") as fh:
        fh.write(full[40:])
        fh.write(line("last line of the old day", "LOG", ""))
    b = cfg.pg_log_dir / "raiden-20261021.json"
    b.write_text(line("invalid page in block 1 of relation base/1/2"))
    kinds = [e.kind for e in t.poll_once()]
    assert kinds == ["crash", "", "corruption"]
    with open(b, "a") as fh:
        fh.write(line("could not read block 3 in file x"))
    assert [e.kind for e in t.poll_once()] == ["corruption"]


def test_tailer_handles_truncation(cfg):
    f = cfg.pg_log_dir / "raiden-20261020.json"
    f.write_text(line("a") * 5)
    t = LogTailer(cfg, from_start=True)
    t.poll_once()
    f.write_text(line("could not open file y"))
    assert [e.kind for e in t.poll_once()] == ["corruption"]


def _liveness(cfg):
    from raiden.watcher import LivenessMonitor

    emitted = []
    return LivenessMonitor(cfg, lambda kind, **kw: emitted.append(kind)), emitted


def test_a_database_that_is_stable_again_stops_being_reported_as_flapping(cfg):
    import asyncio

    mon, emitted = _liveness(cfg)
    q: asyncio.Queue = asyncio.Queue()

    async def scenario():
        for i in range(8):  # a crash storm: up/down every 4 s
            await mon.step(i % 2 == 1, 100.0 + 4 * i, q)
        await mon.step(True, 130.0, q)
        for t in range(140, 1000, 10):  # then it is stable for a quarter of an hour
            await mon.step(True, float(t), q)

    asyncio.run(scenario())
    assert emitted.count("liveness_flapping") == 1  # reported once, never again while stable
    assert [q.get_nowait().kind for _ in range(q.qsize())].count("flapping") == 1


def test_real_ongoing_flapping_is_reported_at_most_every_two_minutes(cfg):
    import asyncio

    mon, emitted = _liveness(cfg)
    q: asyncio.Queue = asyncio.Queue()

    async def scenario():
        for i in range(60):  # 10 minutes of up/down every 10 s
            await mon.step(i % 2 == 1, 100.0 + 10 * i, q)

    asyncio.run(scenario())
    assert 3 <= emitted.count("liveness_flapping") <= 5


def test_a_sustained_outage_is_reported_once_and_recovery_is_noted(cfg):
    import asyncio

    mon, emitted = _liveness(cfg)
    q: asyncio.Queue = asyncio.Queue()

    async def scenario():
        await mon.step(True, 0.0, q)
        for t in range(10, 200, 10):
            await mon.step(False, float(t), q)
        await mon.step(True, 210.0, q)

    asyncio.run(scenario())
    assert emitted.count("liveness_down") == 1 and emitted.count("liveness_up") == 1 and "liveness_flapping" not in emitted
