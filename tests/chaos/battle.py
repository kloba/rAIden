#!/usr/bin/env python3
"""The arena referee. Runs on the lab VM as root, outside rAIden's reach (it lives in /var/lib/arena-private).

  battle.py [--rounds fortify,toast,pitr,choke,lock,toast2,drop2,choke2,lock2,alter,crash,crash2] [--reset] [--warmup 60]

ROUND 0 (fortify) lets rAIden harden the database before anything happens: a verified base backup, a streaming replica,
baseline config and new artifacts. Then FIVE attacks LAND, one at a time: TOAST chunks eaten, DROP TABLE, a report job that
leaks every connection (CHOKEHOLD), a forgotten transaction plus an ALTER TABLE that freeze the application (ICE LOCK) and
finally the pg_crash storm itself. rAIden recovers and evolves a protection after each; the same moves come back (SECOND
CONTACT) and must BOUNCE, and so must a new one (ALTER SYSTEM, stopped by the ROUND 0 baseline). The FINAL ROUND is the
storm: rAIden's finishing move is taking pg_crash out of shared_preload_libraries, and pg_crash's comeback after that
(crash2) must bounce too.

Every verdict comes from the reconcile oracle (every ACKNOWLEDGED commit present, identical, readable) and from
independent probes of the database - never from rAIden's own opinion. The scoreboard is written to
/var/lib/arena/battle/battle.json; nothing in it is hand-edited.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

PY = "/opt/raiden/venv/bin/python"
RAIDEN = Path("/var/lib/raiden")
ARENA = Path("/var/lib/arena")
PRIVATE = Path("/var/lib/arena-private")
OUT = ARENA / "battle"
SCEN = PRIVATE / "scenarios"
EVENTS = RAIDEN / "events.jsonl"
PGLOG = "/var/log/postgresql/raiden-*.json"
BACKUPS = Path("/var/backups/postgresql/base")
CONFD = Path("/etc/postgresql/18/main/conf.d")
DEFAULT_ROUNDS = "fortify,toast,pitr,choke,lock,toast2,drop2,choke2,lock2,alter,crash,crash2"


def agent_model() -> str:
    """The deployment the agent runs on: its own environment file (the referee's environment does not carry it)."""
    if os.environ.get("RAIDEN_AOAI_DEPLOYMENT"):
        return os.environ["RAIDEN_AOAI_DEPLOYMENT"]
    try:
        m = re.search(r"^RAIDEN_AOAI_DEPLOYMENT=(\S+)", Path("/etc/raiden/raiden.env").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(t or time.time(), timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def sh(cmd: list[str] | str, timeout: int = 900, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, **kw)


def as_user(user: str, *cmd: str, timeout: int = 900) -> subprocess.CompletedProcess:
    return sh(["runuser", "-u", user, "--", *cmd], timeout=timeout)


def mark(kind: str, **fields) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rec = {"t": iso(), "type": kind, **fields}
    with open(OUT / "events.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)


def reconcile(amcheck: bool = False) -> dict:
    args = [PY, "/opt/arena/ledger.py", "reconcile", "--json"] + (["--amcheck"] if amcheck else [])
    r = as_user("raiden", *args, timeout=1500)
    try:
        return json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
    except json.JSONDecodeError:
        return {"ok": False, "error": (r.stdout + r.stderr)[-200:]}


def psql(sql: str, db: str = "postgres") -> str:
    r = as_user("raiden", "psql", "-X", "-At", "-d", db, "-c", sql)
    return r.stdout.strip() if r.returncode == 0 else "ERR " + r.stderr.strip()[:200]


def pg_ready() -> bool:
    return sh(["pg_isready", "-q", "-t", "2", "-h", "/var/run/postgresql"]).returncode == 0


def wait_ready(timeout: int = 90) -> bool:
    for _ in range(timeout):
        if pg_ready():
            return True
        time.sleep(1)
    return False


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except FileNotFoundError:
        pass
    return out


def read_events(since: float) -> list[dict]:
    return [e for e in read_jsonl(EVENTS) if parse_iso(e["t"]) >= since]


def crash_lines_since(since: float) -> int:
    files = sorted(glob.glob(PGLOG))
    if not files:
        return 0
    n = 0
    with open(files[-1], "rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - 600_000))
        for raw in fh.read().decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(raw)
                ts = datetime.strptime(d["timestamp"][:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if ts >= since and re.search(r"terminated by signal|exited with exit code (?![01]\b)\d+|all server processes terminated", d.get("message", "")):
                n += 1
    return n


def snapshot_tree() -> dict[str, str]:
    snap = {}
    for root in ("/opt/raiden/.claude/skills", "/opt/raiden/checks.d"):
        for p in sorted(Path(root).rglob("*")):
            if p.is_file():
                snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


# ----------------------------------------------------------------------------------------------- downtime
def probe_loop(stop: threading.Event) -> None:
    """One pg_isready per second for the whole battle: the DB-level view of every outage."""
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / "probe.jsonl", "a") as fh:
        while not stop.is_set():
            t = time.time()
            up = pg_ready()
            fh.write(json.dumps({"t": round(t, 1), "up": up}) + "\n")
            fh.flush()
            stop.wait(max(0.0, 1.0 - (time.time() - t)))


PG_DOWN_START = re.compile(r"all server processes terminated; reinitializing|terminating any other active server processes|received (fast|smart|immediate) shutdown request")
PG_REINIT = re.compile(r"all server processes terminated; reinitializing")
PG_READY = re.compile(r"database system is ready to accept connections")


def pg_log_events(t0: float, t1: float) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for path in sorted(glob.glob(PGLOG))[-2:]:
        with open(path, "rb") as fh:
            for raw in fh.read().decode("utf-8", "replace").splitlines():
                try:
                    d = json.loads(raw)
                    ts = datetime.strptime(d["timestamp"][:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc).timestamp()
                except Exception:
                    continue
                msg = d.get("message", "")
                if t0 - 1 <= ts <= t1 + 60 and (PG_DOWN_START.search(msg) or PG_READY.search(msg)):
                    out.append((ts, msg))
    return sorted(out)


def downtime(t0: float, t1: float) -> dict:
    """What the attack cost, measured twice: the server's own log (a down interval runs from the first 'terminating
    ... processes' / shutdown request to the next 'ready to accept connections', millisecond precision, plus the number of
    crash-recovery cycles) and the application's ledger (the longest stretch without an acknowledged commit, and the total
    time spent in stalls longer than one second). pg_isready once a second is kept as a cross-check."""
    intervals: list[tuple[float, float]] = []
    cycles = 0
    start: float | None = None
    for ts, msg in pg_log_events(t0, t1):
        if PG_DOWN_START.search(msg):
            if start is None and ts <= t1:
                start = ts
            cycles += 1 if PG_REINIT.search(msg) and ts <= t1 else 0
        elif PG_READY.search(msg) and start is not None:
            intervals.append((start, ts))
            start = None
    if start is not None:
        intervals.append((start, t1))
    db_down = sum(b - a for a, b in intervals)
    acks = sorted(r["t"] for r in read_jsonl(ARENA / "ledger.jsonl") if t0 - 3 <= r.get("t", 0) <= t1 + 3)
    gaps = [(a, b - a) for a, b in zip(acks, acks[1:]) if b - a > 1.0]
    probe = [r for r in read_jsonl(OUT / "probe.jsonl") if t0 <= r["t"] <= t1]
    return {
        "crash_cycles": cycles,
        "db_down_s": round(db_down, 1),
        "db_first_down_s": round(intervals[0][0] - t0, 1) if intervals else None,
        "stable_after_s": round(intervals[-1][1] - t0, 1) if intervals else None,
        "db_down_intervals": [[round(a - t0, 1), round(b - t0, 1)] for a, b in intervals[:40]],
        "app_longest_gap_s": round(max((g for _, g in gaps), default=0.0), 1),
        "app_stall_s": round(sum(g - 1.0 for _, g in gaps), 1),
        "app_stalls": len(gaps),
        "probe_down_s": float(sum(1 for r in probe if not r["up"])),
    }


# ----------------------------------------------------------------------------------------------- attacks
def _run(cmd: list[str], timeout: int = 300, env: dict | None = None) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
    return r.returncode, r.stdout + r.stderr


def attack_crash() -> tuple[int, str]:
    return _run(["bash", str(SCEN / "crash_arm.sh")])


def attack_crash_alter() -> tuple[int, str]:
    return _run(["bash", str(SCEN / "crash_alter.sh")])


def attack_bomb(fuse_s: int) -> tuple[int, str]:
    return _run(["bash", str(SCEN / "crash_bomb.sh")], timeout=120, env={"FUSE": str(fuse_s)})


def attack_toast() -> tuple[int, str]:
    return _run([PY, str(SCEN / "toast_corrupt.py")])


def attack_pit() -> tuple[int, str]:
    return _run(["bash", str(SCEN / "pit_drop.sh")])


def attack_choke() -> tuple[int, str]:
    return _run(["bash", str(SCEN / "choke_flood.sh")], timeout=120)


def attack_ice() -> tuple[int, str]:
    return _run(["bash", str(SCEN / "ice_lock.sh")], timeout=120)


def planned_preload() -> str:
    """What the NEXT restart will load (from the config files), as opposed to SHOW shared_preload_libraries."""
    r = as_user("postgres", "/usr/lib/postgresql/18/bin/postgres", "-D", "/etc/postgresql/18/main", "-C", "shared_preload_libraries", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else "ERR " + r.stderr.strip()[:200]


def blocked_error(out: str) -> str | None:
    m = re.search(r"^ATTACK_BLOCKED:\s*(.*)$", out, re.M)
    return m.group(1).strip() if m else None


# ----------------------------------------------------------------------------------------------- verdicts
def verify_crash(t_attack: float) -> tuple[bool, dict]:
    preload = psql("SHOW shared_preload_libraries")
    crashes = crash_lines_since(time.time() - 45)
    rec = reconcile() if "pg_crash" not in preload and crashes == 0 else {"ok": False, "skipped": True}
    return ("pg_crash" not in preload and crashes == 0 and bool(rec.get("ok"))), {"preload": preload, "crash_lines_45s": crashes, "reconcile": rec}


def verify_data(t_attack: float) -> tuple[bool, dict]:
    rec = reconcile()
    stats = json.loads((ARENA / "stats.json").read_text()) if (ARENA / "stats.json").exists() else {}
    return bool(rec.get("ok")) and stats.get("since_last_ack_s", 99) < 15, {"reconcile": rec, "since_last_ack_s": stats.get("since_last_ack_s")}


def verify_toast_blocked(t_attack: float) -> tuple[bool, dict]:
    ok, detail = verify_data(t_attack)
    surgery = psql("SELECT count(*) FROM pg_extension WHERE extname = 'pg_surgery'", "ledger")
    detail["pg_surgery_installed"] = surgery
    return ok and surgery == "0", detail


def verify_drop_blocked(t_attack: float) -> tuple[bool, dict]:
    ok, detail = verify_data(t_attack)
    exists = psql("SELECT to_regclass('public.entries') IS NOT NULL", "ledger")
    detail["entries_exists"] = exists
    return ok and exists == "t", detail


def app_can_connect() -> bool:
    """The application's own view of a connection slot: a plain `SELECT 1` as the application role, not as a superuser."""
    r = sh(["runuser", "-u", "arena", "--", "env", "PGCONNECT_TIMEOUT=3", "psql", "-X", "-At", "-d", "ledger", "-c", "SELECT 1"], timeout=30)
    return r.returncode == 0 and r.stdout.strip() == "1"


def verify_choke(t_attack: float) -> tuple[bool, dict]:
    """Green = the application can open a connection again, it commits again and nothing is lost."""
    can = app_can_connect()
    used = psql("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")
    reporting = psql("SELECT count(*) FROM pg_stat_activity WHERE usename = 'reporting'")
    detail: dict = {"app_can_connect": can, "client_backends": used, "reporting_sessions": reporting, "max_connections": psql("SHOW max_connections")}
    if not can:
        return False, {**detail, "reconcile": {"ok": False, "skipped": True}}
    ok, more = verify_data(t_attack)
    return ok, {**detail, **more}


def verify_lock(t_attack: float) -> tuple[bool, dict]:
    """Green = no session waits for a table lock any more, the application commits again and nothing is lost.
    The oracle reads the table too, so it must not run while the queue is frozen (it would queue behind the ALTER)."""
    waiting = psql("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = 'relation'", "ledger")
    detail: dict = {"ungranted_relation_locks": waiting, "idle_in_transaction": psql("SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction' AND usename = 'analyst'")}
    if waiting != "0":
        return False, {**detail, "reconcile": {"ok": False, "skipped": True}}
    ok, more = verify_data(t_attack)
    return ok, {**detail, **more}


def evidence_crash() -> dict:
    return {
        "preload": psql("SHOW shared_preload_libraries"),
        "source": psql("SELECT coalesce(regexp_replace(sourcefile, '^.*/', ''), '-') || ':' || coalesce(sourceline::text, '-') FROM pg_settings WHERE name = 'shared_preload_libraries'"),
        "experiment_file_present": (CONFD / "50-experiment.conf").exists(),
        "auto_conf_pg_crash_lines": sh(["sudo", "-u", "postgres", "grep", "-c", "pg_crash", "/var/lib/postgresql/18/main/postgresql.auto.conf"]).stdout.strip(),
        "allow_alter_system": psql("SHOW allow_alter_system"),
    }


ROUNDS: dict[str, dict] = {
    "fortify": dict(title="ROUND 0", move="FORTIFY", name="rAIden hardens the database before the fight", special="fortify", timeout=2700),
    "crash": dict(title="FINAL ROUND", move="SIGKILL STORM", name="pg_crash storm", attack=attack_crash, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker"),
    "toast": dict(title="ROUND 1", move="TOAST SNATCH", name="TOAST chunks eaten", attack=attack_toast, verify=verify_data, timeout=1500, clue=r"toast"),
    "pitr": dict(title="ROUND 2", move="DROP KICK", name="DROP TABLE, then point-in-time recovery", attack=attack_pit, verify=verify_data, timeout=2700, clue=r"recovery_target|pitr_clone|point-in-time"),
    "choke": dict(title="ROUND 3", move="CHOKEHOLD", name="a report job leaks every connection", attack=attack_choke, verify=verify_choke, timeout=1500, clue=r"reporting"),
    "lock": dict(title="ROUND 4", move="ICE LOCK", name="a forgotten transaction and an ALTER TABLE freeze the app", attack=attack_ice, verify=verify_lock, timeout=1500, clue=r"analyst|migrator|pg_blocking_pids"),
    "choke2": dict(title="SECOND CONTACT", move="CHOKEHOLD", name="the report job floods again, same move", attack=attack_choke, verify=verify_choke, timeout=900, blocked=True, settle_s=60, repeat_of="choke"),
    "lock2": dict(title="SECOND CONTACT", move="ICE LOCK", name="analyst and migration again, same move", attack=attack_ice, verify=verify_lock, timeout=900, blocked=True, settle_s=60, repeat_of="lock"),
    "crash2": dict(title="SECOND CONTACT", move="SIGKILL STORM", name="pg_crash comes back after the fatality, same move", attack=attack_crash, verify=verify_crash, timeout=900, blocked=True, settle_s=60, repeat_of="crash", evidence=evidence_crash),
    "alter": dict(title="SECOND CONTACT", move="ALTER UPPERCUT", name="pg_crash tries a new move: ALTER SYSTEM", attack=attack_crash_alter, verify=verify_crash, timeout=900, blocked=True, settle_s=60, evidence=evidence_crash),
    "toast2": dict(title="SECOND CONTACT", move="TOAST SNATCH", name="TOAST attack again, same move", attack=attack_toast, verify=verify_toast_blocked, timeout=900, blocked=True, settle_s=60, repeat_of="toast"),
    "drop2": dict(title="SECOND CONTACT", move="DROP KICK", name="DROP TABLE again, same move", attack=attack_pit, verify=verify_drop_blocked, timeout=900, blocked=True, settle_s=60, repeat_of="pitr"),
    # optional, only meaningful BEFORE a pin exists: the config armed now, the restart that activates it scheduled later
    "bomb": dict(title="TIME BOMB", move="TIME BOMB", name="the time bomb: config armed, restart later", attack=None, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker|planned|pending", fuse_s=300, bomb=True),
}


def raiden_state(since: float) -> dict:
    evs = read_events(since)

    def ids(*types: str) -> set[str]:
        return {e["id"] for e in evs if e["type"] in types and "id" in e}

    open_inc = ids("incident_dispatched") - ids("incident_settled")  # settled = verified quiet and evolved (or failed)
    evolving = ids("evolution_started") - ids("evolution_done", "evolution_error")
    last = max((parse_iso(e["t"]) for e in evs if e["type"].startswith(("incident", "evolution", "fortify", "agent_tool", "guard"))), default=0.0)
    return {"open": sorted(open_inc), "evolving": sorted(evolving), "incidents": sum(1 for e in evs if e["type"] == "incident_opened"), "last_activity": last}


def first_clue(inc_ids: list[str], pattern: str, t_attack: float) -> float | None:
    best = None
    rx = re.compile(pattern, re.I)
    for iid in inc_ids:
        path = RAIDEN / "incidents" / iid / "transcript.jsonl"
        if not path.exists():
            continue
        for rec in read_jsonl(path):
            if rx.search(json.dumps(rec)):
                t = parse_iso(rec["t"]) - t_attack
                best = t if best is None else min(best, t)
                break
    return best


def tok(d: dict) -> int:
    u = d.get("usage") or {}
    return int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))


# ----------------------------------------------------------------------------------------------- rounds
def run_round(key: str) -> dict:
    spec = ROUNDS[key]
    t_attack = time.time()
    mark("round_start", round=key, title=spec["title"], move=spec["move"], name=spec["name"])
    rc, out = spec["attack"]()
    err = blocked_error(out)
    saw_effect = bool(re.search(r"^ATTACK_LANDED:", out, re.M))  # the attacker watched its own effect (e.g. the app frozen for 25 s)
    mark("attack_done", round=key, rc=rc, out=out[-400:], blocked_error=err, saw_effect=saw_effect)
    head = {"round": key, "title": spec["title"], "move": spec["move"], "name": spec["name"], "repeat_of": spec.get("repeat_of")}
    if rc != 0:
        mark("round_invalid", round=key, reason="attack_failed")
        return {**head, "won": False, "invalid": "attack_failed", "output": out[-300:]}
    expect_blocked = bool(spec.get("blocked"))
    if expect_blocked or err:
        wait_ready(90)          # the attacker may have restarted the server: judge after the dust has settled
        time.sleep(12)
    else:
        time.sleep(3)
    ok, detail = spec["verify"](t_attack)
    landed = saw_effect or not ok  # a fast defender may already have repaired it when the verdict is taken; the effect was seen
    if not landed:
        evidence = {"blocked_error": err, "attack_output": out[-300:], **detail}
        if spec.get("evidence"):
            evidence.update(spec["evidence"]())
        if expect_blocked or err:
            mark("attack_blocked", round=key, blocked_error=err, evidence=evidence)
            return finish_round(key, spec, t_attack, extra={**head, "blocked": True, "pre_empted": not expect_blocked, "evidence": evidence, "attack_output": out[-300:], "blocked_error": err})
        mark("round_invalid", round=key, reason="attack_ineffective")
        return {**head, "won": False, "invalid": "attack_ineffective", "output": out[-300:]}
    if expect_blocked:
        mark("protection_failed", round=key, detail=detail)
    return finish_round(key, spec, t_attack, extra={**head, "blocked": False, "attack_output": out[-300:]})


def finish_round(key: str, spec: dict, t_attack: float, extra: dict | None = None, min_end: float = 0.0) -> dict:
    """Wait until the oracle is green and rAIden is idle (never before `min_end`), then score the round."""
    first_ok: float | None = None
    passed_since: float | None = None
    detail: dict = {}
    timed_out = True
    deadline = t_attack + spec["timeout"]
    settle = spec.get("settle_s", 150)
    while time.time() < deadline:
        time.sleep(10)
        st = raiden_state(t_attack)
        ok, detail = spec["verify"](t_attack)
        now = time.time()
        if ok:
            first_ok = first_ok or now
            passed_since = passed_since or now
        else:
            passed_since = None
        quiet = now - st["last_activity"] > 30
        (OUT / "status.json").write_text(json.dumps({"round": key, "elapsed_s": int(now - t_attack), "verify_ok": ok, "raiden": st, "t": iso()}))
        if ok and not st["open"] and not st["evolving"] and quiet and now - passed_since >= 20 and now >= min_end and (st["incidents"] >= 1 or now - t_attack > settle):
            timed_out = False
            break
    t_end = time.time()
    evs = read_events(t_attack)
    opened = [e for e in evs if e["type"] == "incident_opened"]
    ids = [e["id"] for e in opened]
    incidents = []
    for iid in ids:
        try:
            incidents.append(json.loads((RAIDEN / "incidents" / iid / "incident.json").read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            incidents.append({"id": iid, "outcome": "unknown"})
    evo = []
    for iid in ids:
        p = RAIDEN / "incidents" / iid / "evolve" / "evolve.json"
        if p.exists():
            evo.append(json.loads(p.read_text()))
    restart_times = [round(r["ts"] - t_attack, 1) for r in read_jsonl(RAIDEN / "restarts.jsonl") if r.get("ts", 0) >= t_attack]
    restarts = len(restart_times)
    window_end = min(t_end, (first_ok or t_end) + 15) if not (extra or {}).get("blocked") else min(t_end, t_attack + 90)
    evolve_done = next((parse_iso(e["t"]) - t_attack for e in evs if e["type"] == "evolution_done"), None)

    result = {
        "round": key, "title": spec["title"], "move": spec["move"], "name": spec["name"], "won": not timed_out, "timed_out": timed_out,
        "attack_at": iso(t_attack), "ended_at": iso(t_end),
        "seconds_to_wake": round(parse_iso(opened[0]["t"]) - t_attack, 1) if opened else None,
        "seconds_to_first_clue": (lambda c: round(c, 1) if c is not None else None)(first_clue(ids, spec.get("clue", "$^"), t_attack)),
        "seconds_to_oracle_ok": round(first_ok - t_attack, 1) if first_ok and not (extra or {}).get("blocked") else None,
        "downtime": downtime(t_attack, window_end),
        "incidents": [{k: i.get(k) for k in ("id", "outcome", "num_turns", "tool_calls", "denied_calls", "duration_ms", "summary", "recurrence_of", "reason")} for i in incidents],
        "recurrences": sum(1 for e in evs if e["type"] == "incident_recurred"),
        "guard_denials": sum(1 for e in evs if e["type"] == "guard_denied"),
        "denied_rules": sorted({e.get("rule", "?") for e in evs if e["type"] == "guard_denied"}),
        "agent_tool_calls": sum(1 for e in evs if e["type"] == "agent_tool"),
        "restarts_used": restarts,
        "restart_at_s": restart_times,
        "evolve_done_s": round(evolve_done, 1) if evolve_done is not None else None,
        "tokens": sum(tok(i) for i in incidents) + sum(tok(e) for e in evo),
        "evolution": [{"changes": e.get("changes"), "summary": e.get("summary"), "turns": e.get("turns")} for e in evo],
        "final_check": detail,
    }
    result.update(extra or {})
    mark("round_end", **{k: result.get(k) for k in ("round", "won", "blocked", "seconds_to_wake", "seconds_to_first_clue", "seconds_to_oracle_ok")}, downtime=result["downtime"])
    return result


def verify_fortify() -> tuple[bool, dict]:
    backups = sorted(p.name for p in BACKUPS.iterdir()) if BACKUPS.is_dir() else []
    newest = backups[-1] if backups else None
    vb = as_user("postgres", "pg_verifybackup", str(BACKUPS / newest)) if newest else None
    repl = psql("SELECT state || '|' || pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint FROM pg_stat_replication ORDER BY 1 LIMIT 1")
    in_rec = as_user("raiden", "psql", "-X", "-At", "-h", "/tmp", "-p", "5434", "-d", "postgres", "-c", "select pg_is_in_recovery()")
    detail = {
        "backups": backups,
        "pg_verifybackup": ((vb.stdout + vb.stderr).strip().splitlines() or [""])[-1] if vb else None,
        "replication": repl,
        "replica_in_recovery": in_rec.stdout.strip(),
        "slot": psql("SELECT slot_name || ' active=' || active FROM pg_replication_slots WHERE slot_name = 'raiden_replica'"),
        "allow_alter_system": psql("SHOW allow_alter_system"),
        "max_slot_wal_keep_size": psql("SHOW max_slot_wal_keep_size"),
        "config_errors": psql("SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL"),
        "reconcile": reconcile(),
    }
    ok = (bool(newest) and vb is not None and vb.returncode == 0 and repl.startswith("streaming") and detail["replica_in_recovery"] == "t"
          and detail["allow_alter_system"] == "off" and detail["config_errors"] == "0" and bool(detail["reconcile"].get("ok")))
    return ok, detail


def run_fortify_round(key: str) -> dict:
    """ROUND 0: nothing is broken. rAIden (daemon stopped, so its own noise cannot open an incident) hardens the server."""
    spec = ROUNDS[key]
    before = snapshot_tree()
    t0 = time.time()
    mark("round_start", round=key, title=spec["title"], move=spec["move"], name=spec["name"])
    sh("systemctl stop raiden || true")
    r = sh("systemctl start raiden-fortify", timeout=spec["timeout"] + 120)
    t1 = time.time()
    evs = read_events(t0)
    done = next((e for e in reversed(evs) if e["type"] == "fortify_done"), None)
    fid = next((e["id"] for e in evs if e["type"] == "fortify_started"), None)
    meta: dict = {}
    if fid:
        try:
            meta = json.loads((RAIDEN / "incidents" / fid / "incident.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    ok, detail = verify_fortify()
    after = snapshot_tree()
    added = sorted(p.replace("/opt/raiden/", "") for p, h in after.items() if before.get(p) != h)
    try:
        protections = [ln for ln in (RAIDEN / "memory" / "protections.list").read_text().splitlines() if ln.strip()]
    except FileNotFoundError:
        protections = []
    result = {
        "round": key, "title": spec["title"], "move": spec["move"], "name": spec["name"], "won": bool(ok and done and done.get("outcome") == "resolved"),
        "fortify": True, "attack_at": iso(t0), "ended_at": iso(t1), "seconds_total": round(t1 - t0, 1),
        "session_seconds": done.get("seconds") if done else None, "outcome": done.get("outcome") if done else None,
        "turns": done.get("turns") if done else None, "tool_calls": done.get("tool_calls") if done else None,
        "denied_calls": done.get("denied") if done else None, "tokens": tok(meta), "fortify_id": fid,
        "artifacts_added": added, "protections": protections, "downtime": downtime(t0, t1), "final_check": detail,
        "unit_rc": r.returncode, "restarts_used": sum(1 for x in read_jsonl(RAIDEN / "restarts.jsonl") if x.get("ts", 0) >= t0),
    }
    mark("round_end", round=key, won=result["won"], seconds=result["seconds_total"], artifacts=len(added), protections=len(protections))
    return result


def run_bomb_round(key: str) -> dict:
    """THE TIME BOMB (optional). The attack only ARMS a crash (config on disk + reload + a restart scheduled `fuse_s` from now).
    Nothing is broken until the fuse fires, so the defender wins by noticing what the next restart would load and
    fixing it first. If the fuse fires with pg_crash still planned, the crash storm is a normal fight."""
    spec = ROUNDS[key]
    fuse = spec["fuse_s"]
    t_attack = time.time()
    mark("round_start", round=key, title=spec["title"], name=spec["name"], fuse_s=fuse)
    rc, out = attack_bomb(fuse)
    mark("attack_done", round=key, rc=rc, out=out[-400:])
    planned = planned_preload()
    head = {"round": key, "title": spec["title"], "move": spec["move"], "name": spec["name"]}
    invalid = "attack_failed" if rc != 0 else (None if "pg_crash" in planned else "attack_ineffective")
    if invalid:
        mark("round_invalid", round=key, reason=invalid, planned=planned)
        return {**head, "won": False, "invalid": invalid, "output": out[-300:]}
    t_fuse = t_attack + fuse
    mark("bomb_armed", round=key, planned_preload=planned, fuse_at=iso(t_fuse))
    defused_at: float | None = None
    while time.time() < t_fuse + 45:
        if defused_at is None and "pg_crash" not in planned_preload():
            defused_at = time.time()
            mark("bomb_defused", round=key, seconds=round(defused_at - t_attack, 1), planned_preload=planned_preload())
        time.sleep(4)
    try:
        started = float(psql("SELECT extract(epoch FROM pg_postmaster_start_time())"))
    except ValueError:
        started = 0.0
    if started < t_fuse - 5:  # the scheduled restart never happened: nothing was tested
        mark("round_invalid", round=key, reason="fuse_did_not_fire")
        return {**head, "won": False, "invalid": "fuse_did_not_fire"}
    preload_now = psql("SHOW shared_preload_libraries")
    crashes = crash_lines_since(t_fuse - 5)
    common = {**head, "armed_at": iso(t_attack), "fuse_s": fuse, "defused_s": None if defused_at is None else round(defused_at - t_attack, 1)}
    if "pg_crash" in preload_now or crashes > 0:
        mark("bomb_exploded", round=key, preload=preload_now, crash_lines=crashes)
        return finish_round(key, spec, t_fuse, extra={**common, "prevented": False, "exploded": True, "armed_at_note": "timings below are measured from the explosion"})
    ev = {"preload_after_restart": preload_now, "planned_before_fuse": planned, "crash_lines_after_fuse": crashes, "restarted_at": iso(started)}
    mark("attack_prevented", round=key, **ev)
    return finish_round(key, spec, t_attack, extra={**common, "prevented": True, "evidence": ev}, min_end=t_fuse + 60)


# ----------------------------------------------------------------------------------------------- lifecycle
def reset_state() -> None:
    mark("reset_begin")
    sh("systemctl stop raiden raiden-fortify pg-standby 2>/dev/null || true")
    sh("pkill -f 'ledger.py run' || true")
    sh("rm -f /etc/postgresql/18/main/conf.d/50-experiment.conf /etc/postgresql/18/main/conf.d/9[0-9]-raiden-*.conf")
    sh("pkill -u reporting; pkill -u analyst; pkill -u migrator; true")  # leftovers of CHOKEHOLD / ICE LOCK (the attackers' own processes)
    sh("systemctl stop arena-maintenance-restart.timer arena-maintenance-restart.service 2>/dev/null; systemctl reset-failed 2>/dev/null; true")
    # the previous run's protections live inside the database: drop them before the table is recreated
    sh("runuser -u postgres -- psql -X -d ledger -c 'DROP EVENT TRIGGER IF EXISTS raiden_protect_tables' -c 'DROP EVENT TRIGGER IF EXISTS raiden_extension_allowlist' "
       "-c 'DROP SCHEMA IF EXISTS raiden CASCADE' -c 'DROP EXTENSION IF EXISTS pg_surgery' 2>&1 | tail -3")
    sh("printf '%s\\n' '# Do not edit this file manually!' '# It will be overwritten by the ALTER SYSTEM command.' > /var/lib/postgresql/18/main/postgresql.auto.conf "
       "&& chown postgres:postgres /var/lib/postgresql/18/main/postgresql.auto.conf && chmod 600 /var/lib/postgresql/18/main/postgresql.auto.conf")
    sh("journalctl --rotate; journalctl --vacuum-time=1s")  # forget the previous run's sudo trail
    sh("mkdir -p /var/log/postgresql/old && mv /var/log/postgresql/raiden-* /var/log/postgresql/old/ 2>/dev/null; true")
    sh("systemctl restart postgresql@18-main")
    for _ in range(60):
        if pg_ready():
            break
        time.sleep(1)
    # replication slots, base backups and archived WAL of the previous run: the lab starts like a server with no backups
    sh("runuser -u postgres -- psql -X -At -d postgres -c \"SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots\" 2>&1 | tail -2")
    sh("rm -rf /var/backups/postgresql/base/* /var/backups/postgresql/base/.[!.]* 2>/dev/null; rm -f /var/lib/postgresql/wal_archive/* 2>/dev/null; true")
    sh("mkdir -p /var/lib/arena/old && for f in ledger.jsonl stats.json timeline.jsonl; do [ -f /var/lib/arena/$f ] && mv /var/lib/arena/$f /var/lib/arena/old/$f.$(date +%s); done; true")
    sh("runuser -u arena -- psql -X -d ledger -c 'DROP TABLE IF EXISTS entries'")
    r = as_user("arena", PY, "/opt/arena/ledger.py", "init")
    mark("schema", out=(r.stdout + r.stderr).strip())
    # what a previous run's protections did to the troublemakers' roles (limits, timeouts), then what they may read
    sh("runuser -u postgres -- psql -X -d ledger -c 'ALTER ROLE reporting RESET ALL' -c 'ALTER ROLE reporting CONNECTION LIMIT -1' -c 'ALTER ROLE analyst RESET ALL' "
       "-c 'ALTER ROLE analyst CONNECTION LIMIT -1' -c 'ALTER ROLE migrator RESET ALL' -c 'ALTER ROLE migrator CONNECTION LIMIT -1' -c 'GRANT SELECT ON public.entries TO analyst' 2>&1 | tail -3")
    for d in ("incidents", "memory", "escalations", "quarantine", "work"):
        sh(f"rm -rf /var/lib/raiden/{d}/* /var/lib/raiden/{d}/.[!.]* 2>/dev/null; true")
    sh("rm -f /var/lib/raiden/events.jsonl /var/lib/raiden/audit.jsonl /var/lib/raiden/restarts.jsonl")
    if Path("/opt/raiden-src/.claude/skills").is_dir():
        sh("rsync -a --delete /opt/raiden-src/.claude/skills/ /opt/raiden/.claude/skills/ && rsync -a --delete /opt/raiden-src/checks.d/ /opt/raiden/checks.d/ && chown -R raiden:raiden /opt/raiden/.claude /opt/raiden/checks.d")
    sh("ls -1 /etc/postgresql/18/main/conf.d/")
    mark("reset_done")


def ensure_atomic_archive() -> None:
    conf = Path("/etc/postgresql/18/main/conf.d/20-archive-atomic.conf")
    if not conf.exists():
        conf.write_text("archive_command = 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f.tmp && mv /var/lib/postgresql/wal_archive/%f.tmp /var/lib/postgresql/wal_archive/%f'\n")
        sh(["chown", "postgres:postgres", str(conf)])
        sh("systemctl reload postgresql@18-main")


def start_workload() -> None:
    sh("nohup runuser -u arena -- " + PY + " /opt/arena/ledger.py run > /var/lib/arena/workload.log 2>&1 &", timeout=20)
    mark("workload_started")


def stop_workload() -> dict:
    sh("pkill -TERM -f 'ledger.py run' || true")
    time.sleep(6)
    try:
        return json.loads((ARENA / "stats.json").read_text())
    except Exception:
        return {}


def fresh_base_backup() -> str:
    dest = "/var/backups/postgresql/base/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    r = sh(["sudo", "-u", "postgres", "pg_basebackup", "-D", dest, "-Fp", "-X", "stream", "-c", "fast"], timeout=900)
    mark("base_backup", dest=dest, rc=r.returncode, err=r.stderr[-200:])
    return dest


def collect(t_start: float, payload: dict) -> None:
    (OUT / "battle.json").write_text(json.dumps(payload, indent=2))
    tl = []
    try:
        bucket: dict[int, int] = {}
        for d in read_jsonl(ARENA / "timeline.jsonl"):
            if d["t"] >= t_start - 60:
                b = int((d["t"] - t_start) // 10) * 10
                bucket[b] = bucket.get(b, 0) + d["delta"]
        tl = sorted(bucket.items())
    except FileNotFoundError:
        pass
    (OUT / "timeline_10s.json").write_text(json.dumps(tl))
    with tarfile.open(OUT / "artifacts.tgz", "w:gz") as tar:
        for rel in ("incidents", "memory", "events.jsonl", "audit.jsonl", "restarts.jsonl", "escalations", "quarantine"):
            p = RAIDEN / rel
            if p.exists():
                tar.add(p, arcname=f"raiden/{rel}")
        tar.add(OUT / "events.jsonl", arcname="arena/events.jsonl")
        tar.add(OUT / "timeline_10s.json", arcname="arena/timeline_10s.json")
        tar.add(OUT / "battle.json", arcname="arena/battle.json")
        for p in Path("/opt/raiden/.claude/skills").iterdir():
            tar.add(p, arcname=f"skills/{p.name}")
        tar.add("/opt/raiden/checks.d", arcname="checks.d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", default=DEFAULT_ROUNDS)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--warmup", type=int, default=60)
    ap.add_argument("--backup", action="store_true", help="referee takes a base backup after the warm-up (only for runs without the fortify round)")
    args = ap.parse_args()
    keys = [k.strip() for k in args.rounds.split(",") if k.strip()]
    unknown = [k for k in keys if k not in ROUNDS]
    if unknown or "fortify" in keys[1:]:
        print(f"unknown rounds: {unknown}; known: {sorted(ROUNDS)}; 'fortify' can only be the first round", file=sys.stderr)
        return 64
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.jsonl").write_text("")
    (OUT / "probe.jsonl").write_text("")
    t_start = time.time()
    mark("battle_start", rounds=args.rounds)
    if args.reset:
        reset_state()
    ensure_atomic_archive()
    stop_probe = threading.Event()
    threading.Thread(target=probe_loop, args=(stop_probe,), daemon=True).start()
    start_workload()
    time.sleep(args.warmup)
    if args.backup:
        fresh_base_backup()
    before = snapshot_tree()
    results: list[dict] = []

    def put(res: dict) -> bool:
        results.append(res)
        collect(t_start, {"in_progress": True, "rounds": results})
        if res.get("invalid"):
            mark("battle_aborted", reason=res["invalid"])
            stop_probe.set()
            stop_workload()
            return False
        return True

    if keys and keys[0] == "fortify":
        if not put(run_fortify_round("fortify")):
            return 2
        keys = keys[1:]
        time.sleep(10)
    sh("systemctl start raiden")
    time.sleep(15)
    mark("raiden_on_duty", ready=pg_ready(), baseline=reconcile())
    for key in keys:
        res = run_bomb_round(key) if ROUNDS[key].get("bomb") else run_round(key)
        if not put(res):
            return 2
        time.sleep(30)
    stats = stop_workload()
    stop_probe.set()
    final = reconcile(amcheck=True)
    after = snapshot_tree()
    changed = sorted(p.replace("/opt/raiden/", "") for p, h in after.items() if before.get(p) != h)
    try:
        protections = [ln for ln in (RAIDEN / "memory" / "protections.list").read_text().splitlines() if ln.strip()]
    except FileNotFoundError:
        protections = []
    blocked = [r for r in results if r.get("blocked")]
    payload = {
        "started_at": iso(t_start), "finished_at": iso(), "duration_s": int(time.time() - t_start),
        "rounds": results, "workload": stats, "final": final,
        "flawless": bool(final.get("ok")) and final.get("lost") == 0 and all(r["won"] for r in results),
        "rounds_won": sum(1 for r in results if r["won"]), "rounds_total": len(results),
        "attacks_landed": sum(1 for r in results if r.get("blocked") is False), "attacks_blocked": len(blocked),
        "blocked_attempts": sum(1 for r in results if "second contact" in r.get("title", "").lower()),
        "artifacts_added_or_changed": changed, "protections": protections,
        "pg_version": psql("SHOW server_version"),
        "model": agent_model(),
    }
    mark("battle_end", flawless=payload["flawless"], final=final)
    collect(t_start, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
