import json

import pytest

from raiden.incident import CRITICAL_WEIGHT, Aggregator, classify, normalize, parse_jsonlog_line


def jl(message, sev="ERROR", state="", user="", ts="2026-10-20 05:00:00.123 UTC", **extra):
    return json.dumps({"timestamp": ts, "error_severity": sev, "state_code": state, "message": message, "pid": 4242, "backend_type": "client backend", "user": user, **extra})


CASES = [
    ("server process (PID 4242) was terminated by signal 9: Killed", "LOG", "", "", "crash", 100),
    ("all server processes terminated; reinitializing", "LOG", "", "", "crash", 100),
    ("unexpected chunk number 2 (expected 1) for toast value 16777 in pg_toast_16414", "ERROR", "XX001", "", "corruption", 100),
    ("invalid page in block 7 of relation base/16384/16385", "ERROR", "XX001", "", "corruption", 100),
    ("statement: DROP TABLE public.entries", "LOG", "", "arena", "destructive_ddl", 100),
    ("statement: TRUNCATE entries", "LOG", "", "arena", "destructive_ddl", 100),
    ("statement: DROP TABLE IF EXISTS public.scratch_lock_probe", "LOG", "", "raiden", "", 0),  # the agent's own scratch clean-up
    ("statement: DROP TABLE public.pitr_entries", "LOG", "", "raiden", "", 0),
    ("statement: DROP TABLE public.scratch_x", "LOG", "", "arena", "destructive_ddl", 100),  # anybody else's still pages
    ("statement: DROP TABLE public.entries", "LOG", "", "raiden", "destructive_ddl", 100),  # and so does the agent's own real DROP
    ("could not extend file \"base/1/2\": No space left on device", "ERROR", "53100", "", "resource", 60),
    ("archive command failed with exit code 1", "LOG", "", "", "archive_failure", 60),
    ("process 4242 still waiting for RowExclusiveLock on relation 16431 of database 16430 after 1000.115 ms", "LOG", "", "arena", "lock_queue", 60),
    ("process 4242 still waiting for AccessExclusiveLock on relation 16431 of database 16430 after 1000.087 ms", "LOG", "", "migrator", "lock_queue", 60),
    ("process 4242 still waiting for ShareLock on transaction 9001 after 1000.050 ms", "LOG", "", "arena", "", 0),  # row-level waits are ordinary OLTP
    ("process 4242 acquired RowExclusiveLock on relation 16431 of database 16430 after 61234.567 ms", "LOG", "", "arena", "", 0),
    ("process 4242 still waiting for RowExclusiveLock on relation 16431 of database 16430 after 1000.115 ms", "LOG", "", "raiden", "", 0),  # the agent's own waits never wake the agent
    ("sorry, too many clients already", "FATAL", "53300", "", "resource", 60),
    ('too many connections for role "reporting"', "FATAL", "53300", "reporting", "role_limit", 5),  # a capped role, the server has free slots
    ("database is not accepting commands that assign new transaction IDs to avoid wraparound data loss", "ERROR", "54000", "", "wraparound", 100),
    ("terminating connection due to administrator command", "FATAL", "57P01", "", "fatal_admin", 5),
    ("the database system is in recovery mode", "FATAL", "57P03", "", "recovery_mode", 3),
    ('relation "entries" does not exist', "ERROR", "42P01", "arena", "missing_relation", 3),
    ("deadlock detected", "ERROR", "40P01", "", "error", 10),
    ("duplicate key value violates unique constraint", "ERROR", "23505", "", "", 0),
    ("syntax error at or near", "ERROR", "42601", "", "", 0),
    ("canceling statement due to user request", "ERROR", "57014", "", "", 0),
    ("checkpoint starting: time", "LOG", "", "", "", 0),
    ("received fast shutdown request", "LOG", "", "", "admin_shutdown", 0),
    ("received immediate shutdown request", "LOG", "", "", "admin_shutdown", 0),
    ('background worker "logical replication launcher" (PID 4242) exited with exit code 1', "LOG", "", "", "", 0),  # every clean shutdown
    ('background worker "logical replication launcher" (PID 4242) exited with exit code 0', "LOG", "", "", "", 0),
    ('background worker "crash worker" (PID 4242) exited with exit code 2', "LOG", "", "", "crash", 100),
    ("server process (PID 4242) exited with exit code 10", "LOG", "", "", "crash", 100),
    ("received SIGHUP, reloading configuration files", "LOG", "", "", "config_reload", 0),
    ('parameter "shared_preload_libraries" cannot be changed without restarting the server', "LOG", "", "", "config_reload", 0),
    ('configuration file "/etc/postgresql/18/main/postgresql.conf" contains errors; unaffected changes were applied', "LOG", "", "", "config_reload", 0),
    ('relation "x" does not exist', "ERROR", "42P01", "raiden", "", 0),  # the agent's own mistakes never wake the agent
    ("terminating connection due to administrator command", "FATAL", "57P01", "raiden", "", 0),
]


@pytest.mark.parametrize("message,sev,state,user,kind,weight", CASES)
def test_classification(message, sev, state, user, kind, weight):
    ev = classify(parse_jsonlog_line(jl(message, sev, state, user)))
    assert (ev.kind, ev.weight) == (kind, weight)
    assert bool(ev.signature) == bool(kind)


def test_garbage_lines_are_skipped():
    assert parse_jsonlog_line("") is None
    assert parse_jsonlog_line("not json at all") is None


def test_pids_do_not_split_signatures():
    a = classify(parse_jsonlog_line(jl("server process (PID 4242) was terminated by signal 9: Killed", "LOG")))
    b = classify(parse_jsonlog_line(jl("server process (PID 98765) was terminated by signal 9: Killed", "LOG")))
    assert a.signature == b.signature
    assert normalize("pid 123456") == "pid N"


def ev(message, sev="ERROR", state="", user="", t=0.0):
    e = classify(parse_jsonlog_line(jl(message, sev, state, user)))
    e.ts = t
    return e


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make_agg(clock):
    return Aggregator(trigger_weight=10, quiet_s=8, max_wait_s=30, suppress_s=300, clock=clock)


def test_critical_event_opens_a_window_and_closes_after_quiet():
    c = Clock()
    agg = make_agg(c)
    assert agg.feed(ev("server process (PID 1111) was terminated by signal 9: Killed", "LOG"))
    assert agg.poll() is None
    c.t = 7.9
    assert agg.poll() is None
    c.t = 8.1
    inc = agg.poll()
    assert inc and inc.critical and inc.kinds == {"crash": 1} and inc.id.startswith("inc-")
    assert agg.poll() is None


def test_low_weight_noise_accumulates_to_a_trigger():
    c = Clock()
    agg = make_agg(c)
    for i in range(3):
        c.t = i
        agg.feed(ev('relation "t" does not exist', "ERROR", "42P01", "arena"))
    c.t = 20
    assert agg.poll() is None  # 3 x 3 = 9 < 10
    c.t = 21
    agg.feed(ev('relation "t" does not exist', "ERROR", "42P01", "arena"))
    c.t = 40
    inc = agg.poll()
    assert inc and not inc.critical and inc.events_total == 4


def test_a_crash_storm_is_one_incident():
    c = Clock()
    agg = make_agg(c)
    for i in range(400):
        c.t = i * 0.05
        agg.feed(ev(f"server process (PID {1000 + i}) was terminated by signal 9: Killed", "LOG"))
    c.t = 20 + 9
    inc = agg.poll()
    assert inc and inc.events_total == 400 and len(inc.signatures) == 1
    assert next(iter(inc.signatures.values()))["count"] == 400
    assert len(inc.sample) <= 6


def test_max_wait_closes_a_window_that_never_goes_quiet():
    c = Clock()
    agg = make_agg(c)
    for i in range(35):
        c.t = float(i)
        agg.feed(ev("server process (PID 1234) was terminated by signal 9: Killed", "LOG"))
    c.t = 35
    assert agg.poll() is not None  # first event at t=0, max_wait 30 s


def test_handled_app_errors_are_muted_but_critical_never_is():
    c = Clock()
    agg = make_agg(c)
    for i in range(4):
        c.t = i
        agg.feed(ev("deadlock detected", "ERROR", "40P01"))
    c.t = 30
    inc = agg.poll()
    assert inc
    agg.mute(inc)
    assert not agg.feed(ev("deadlock detected", "ERROR", "40P01"))  # muted
    assert agg.suppressed_count == 1
    assert agg.feed(ev("server process (PID 1) was terminated by signal 9: Killed", "LOG"))  # never muted
    c.t = 30 + 301
    assert agg.feed(ev("deadlock detected", "ERROR", "40P01"))  # the mute expired


def test_discard_open_variants():
    c = Clock()
    agg = make_agg(c)
    crash = ev("server process (PID 1) was terminated by signal 9: Killed", "LOG")
    noise = ev("terminating connection due to administrator command", "FATAL", "57P01")
    down = ev("x", "LOG")
    down.kind, down.weight, down.signature = "service_down", 100, "service_down:x"
    for e in (crash, noise, down):
        agg.feed(e)
    agg.discard_open({crash.signature}, drop_noncritical=True, drop_kinds={"service_down"})
    assert not agg.has_open_window()
    # a NEW critical kind survives
    agg.feed(crash)
    other = ev("unexpected chunk number 2 (expected 1) for toast value 1 in pg_toast_2", "ERROR", "XX001")
    agg.feed(other)
    agg.discard_open({crash.signature}, drop_noncritical=True)
    c.t = 50
    inc = agg.poll()
    assert inc and set(inc.kinds) == {"corruption"}
    agg.feed(crash)
    agg.discard_open()
    assert not agg.has_open_window()


def test_critical_weight_constant():
    assert CRITICAL_WEIGHT == 60
