#!/usr/bin/env python3
"""The arena referee. Runs on the lab VM as root, outside rAIden's reach (it lives in /var/lib/arena-private).

  battle.py [--rounds crash,toast,pitr,bomb,boss2] [--reset] [--warmup 60]

It starts the ledger workload, takes a fresh base backup, fires one attack per round, and judges each round with
the reconcile oracle (every ACKNOWLEDGED commit present, identical, readable) - never with rAIden's own opinion.
The scoreboard is written to /var/lib/arena/battle/battle.json; nothing in it is hand-edited.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
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
    return sh(["pg_isready", "-q", "-h", "/var/run/postgresql"]).returncode == 0


def read_events(since: float) -> list[dict]:
    out = []
    try:
        for line in EVENTS.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if parse_iso(e["t"]) >= since:
                out.append(e)
    except FileNotFoundError:
        pass
    return out


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


# ----------------------------------------------------------------------------------------------- attacks
def attack_crash() -> tuple[int, str]:
    r = sh(["bash", str(SCEN / "crash_arm.sh")], timeout=300)
    return r.returncode, r.stdout + r.stderr


def attack_crash_alter() -> tuple[int, str]:
    r = sh(["bash", str(SCEN / "crash_alter.sh")], timeout=300)
    return r.returncode, r.stdout + r.stderr


def attack_bomb(fuse_s: int) -> tuple[int, str]:
    r = sh(["env", f"FUSE={fuse_s}", "bash", str(SCEN / "crash_bomb.sh")], timeout=120)
    return r.returncode, r.stdout + r.stderr


def planned_preload() -> str:
    """What the NEXT restart will load (from the config files), as opposed to SHOW shared_preload_libraries."""
    r = as_user("postgres", "/usr/lib/postgresql/18/bin/postgres", "-D", "/etc/postgresql/18/main", "-C", "shared_preload_libraries", timeout=60)
    return r.stdout.strip() if r.returncode == 0 else "ERR " + r.stderr.strip()[:200]


def attack_toast() -> tuple[int, str]:
    r = sh([PY, str(SCEN / "toast_corrupt.py")], timeout=300)
    return r.returncode, r.stdout + r.stderr


def attack_pit() -> tuple[int, str]:
    r = sh(["bash", str(SCEN / "pit_drop.sh")], timeout=300)
    return r.returncode, r.stdout + r.stderr


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


ROUNDS = {
    "crash": dict(title="ROUND 1", name="pg_crash storm", attack=attack_crash, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker"),
    "toast": dict(title="TOASTY!", name="TOAST chunks eaten", attack=attack_toast, verify=verify_data, timeout=1500, clue=r"toast"),
    "pitr": dict(title="THE PIT", name="DROP TABLE -> point-in-time recovery", attack=attack_pit, verify=verify_data, timeout=2700, clue=r"recovery_target|pitr_clone|point-in-time"),
    "rematch": dict(title="REMATCH", name="pg_crash returns (same move)", attack=attack_crash, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker", prevent_ok=True),
    "bomb": dict(title="REMATCH", name="the time bomb: config armed, restart later", attack=None, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker|planned|pending", fuse_s=300, bomb=True),
    "boss2": dict(title="BOSS RETURNS", name="pg_crash returns (new move: ALTER SYSTEM)", attack=attack_crash_alter, verify=verify_crash, timeout=1500, clue=r"pg_crash|crash worker|auto\.conf|alter system", prevent_ok=True),
}


def raiden_state(since: float) -> dict:
    evs = read_events(since)

    def ids(*types: str) -> set[str]:
        return {e["id"] for e in evs if e["type"] in types and "id" in e}

    open_inc = ids("incident_dispatched") - ids("incident_settled")  # settled = verified quiet and evolved (or failed)
    evolving = ids("evolution_started") - ids("evolution_done", "evolution_error")
    last = max((parse_iso(e["t"]) for e in evs if e["type"].startswith(("incident", "evolution", "agent_tool", "guard"))), default=0.0)
    return {"open": sorted(open_inc), "evolving": sorted(evolving), "incidents": sum(1 for e in evs if e["type"] == "incident_opened"), "last_activity": last}


def first_clue(inc_ids: list[str], pattern: str, t_attack: float) -> float | None:
    best = None
    rx = re.compile(pattern, re.I)
    for iid in inc_ids:
        path = RAIDEN / "incidents" / iid / "transcript.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            blob = json.dumps(rec)
            if rx.search(blob):
                t = parse_iso(rec["t"]) - t_attack
                best = t if best is None else min(best, t)
                break
    return best


def run_round(key: str) -> dict:
    spec = ROUNDS[key]
    t_attack = time.time()
    mark("round_start", round=key, title=spec["title"], name=spec["name"])
    rc, out = spec["attack"]()
    mark("attack_done", round=key, rc=rc, out=out[-400:])
    invalid = None
    if rc != 0:
        invalid = "attack_failed"
    else:
        time.sleep(3)
        landed = not spec["verify"](t_attack)[0]  # right after the attack the oracle must NOT be happy
        if not landed and spec.get("prevent_ok"):
            # an evolved defender may neutralise the move before it bites: that is a win, and the evidence is recorded
            ev = {"preload": psql("SHOW shared_preload_libraries"), "experiment_file_present": Path("/etc/postgresql/18/main/conf.d/50-experiment.conf").exists(),
                  "auto_conf": sh(["sudo", "-u", "postgres", "grep", "-c", "pg_crash", "/var/lib/postgresql/18/main/postgresql.auto.conf"]).stdout.strip()}
            mark("attack_prevented", round=key, **ev)
            time.sleep(20)
            st = raiden_state(t_attack)
            return {"round": key, "title": spec["title"], "name": spec["name"], "won": True, "prevented": True, "evidence": ev,
                    "incidents_triggered": st["incidents"], "attack_at": iso(t_attack), "ended_at": iso()}
        if not landed:
            invalid = "attack_ineffective"
    if invalid:
        mark("round_invalid", round=key, reason=invalid)
        return {"round": key, "title": spec["title"], "name": spec["name"], "won": False, "invalid": invalid, "output": out[-300:]}
    return finish_round(key, spec, t_attack)


def finish_round(key: str, spec: dict, t_attack: float, extra: dict | None = None, min_end: float = 0.0) -> dict:
    """Wait until the oracle is green and rAIden is idle (never before `min_end`), then score the round."""
    first_ok: float | None = None
    passed_since: float | None = None
    detail: dict = {}
    timed_out = True
    deadline = t_attack + spec["timeout"]
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
        if ok and not st["open"] and not st["evolving"] and quiet and now - passed_since >= 20 and now >= min_end and (st["incidents"] >= 1 or now - t_attack > 150):
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
    restarts = 0
    try:
        restarts = sum(1 for line in (RAIDEN / "restarts.jsonl").read_text().splitlines() if json.loads(line)["ts"] >= t_attack)
    except FileNotFoundError:
        pass

    def tok(d: dict) -> int:
        u = d.get("usage") or {}
        return int(u.get("prompt_tokens", 0)) + int(u.get("completion_tokens", 0))

    result = {
        "round": key, "title": spec["title"], "name": spec["name"], "won": not timed_out, "timed_out": timed_out,
        "attack_at": iso(t_attack), "ended_at": iso(t_end),
        "seconds_to_wake": round(parse_iso(opened[0]["t"]) - t_attack, 1) if opened else None,
        "seconds_to_first_clue": (lambda c: round(c, 1) if c is not None else None)(first_clue(ids, spec["clue"], t_attack)),
        "seconds_to_oracle_ok": round(first_ok - t_attack, 1) if first_ok else None,
        "incidents": [{k: i.get(k) for k in ("id", "outcome", "num_turns", "tool_calls", "denied_calls", "duration_ms", "summary", "recurrence_of", "reason")} for i in incidents],
        "recurrences": sum(1 for e in evs if e["type"] == "incident_recurred"),
        "guard_denials": sum(1 for e in evs if e["type"] == "guard_denied"),
        "denied_rules": sorted({e.get("rule", "?") for e in evs if e["type"] == "guard_denied"}),
        "agent_tool_calls": sum(1 for e in evs if e["type"] == "agent_tool"),
        "restarts_used": restarts,
        "tokens": sum(tok(i) for i in incidents) + sum(tok(e) for e in evo),
        "evolution": [{"changes": e.get("changes"), "summary": e.get("summary"), "turns": e.get("turns")} for e in evo],
        "final_check": detail,
    }
    result.update(extra or {})
    mark("round_end", **{k: result[k] for k in ("round", "won", "seconds_to_wake", "seconds_to_first_clue", "seconds_to_oracle_ok")})
    return result


def run_bomb_round(key: str) -> dict:
    """THE TIME BOMB. The attack only ARMS a crash (config on disk + reload + a restart scheduled `fuse_s` from now).
    Nothing is broken until the fuse fires, so the defender wins by noticing what the next restart would load and
    fixing it first. If the fuse fires with pg_crash still planned, the crash storm is a normal fight."""
    spec = ROUNDS[key]
    fuse = spec["fuse_s"]
    t_attack = time.time()
    mark("round_start", round=key, title=spec["title"], name=spec["name"], fuse_s=fuse)
    rc, out = attack_bomb(fuse)
    mark("attack_done", round=key, rc=rc, out=out[-400:])
    planned = planned_preload()
    invalid = "attack_failed" if rc != 0 else (None if "pg_crash" in planned else "attack_ineffective")
    if invalid:
        mark("round_invalid", round=key, reason=invalid, planned=planned)
        return {"round": key, "title": spec["title"], "name": spec["name"], "won": False, "invalid": invalid, "output": out[-300:]}
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
        return {"round": key, "title": spec["title"], "name": spec["name"], "won": False, "invalid": "fuse_did_not_fire"}
    preload_now = psql("SHOW shared_preload_libraries")
    crashes = crash_lines_since(t_fuse - 5)
    common = {"armed_at": iso(t_attack), "fuse_s": fuse, "defused_s": None if defused_at is None else round(defused_at - t_attack, 1)}
    if "pg_crash" in preload_now or crashes > 0:
        mark("bomb_exploded", round=key, preload=preload_now, crash_lines=crashes)
        return finish_round(key, spec, t_fuse, extra={**common, "prevented": False, "exploded": True, "armed_at_note": "timings below are measured from the explosion"})
    ev = {"preload_after_restart": preload_now, "planned_before_fuse": planned, "crash_lines_after_fuse": crashes, "restarted_at": iso(started)}
    mark("attack_prevented", round=key, **ev)
    return finish_round(key, spec, t_attack, extra={**common, "prevented": True, "evidence": ev, "seconds_to_oracle_ok": None}, min_end=t_fuse + 60)


# ----------------------------------------------------------------------------------------------- lifecycle
def reset_state() -> None:
    mark("reset_begin")
    sh("systemctl stop raiden || true")
    sh("pkill -f 'ledger.py run' || true")
    sh("rm -f /etc/postgresql/18/main/conf.d/50-experiment.conf /etc/postgresql/18/main/conf.d/99-raiden-*.conf")
    sh("systemctl stop arena-maintenance-restart.timer arena-maintenance-restart.service 2>/dev/null; systemctl reset-failed 2>/dev/null; true")
    sh("printf '%s\\n' '# Do not edit this file manually!' '# It will be overwritten by the ALTER SYSTEM command.' > /var/lib/postgresql/18/main/postgresql.auto.conf "
       "&& chown postgres:postgres /var/lib/postgresql/18/main/postgresql.auto.conf && chmod 600 /var/lib/postgresql/18/main/postgresql.auto.conf")
    sh("journalctl --rotate; journalctl --vacuum-time=1s")  # forget the previous run's sudo trail
    sh("mkdir -p /var/log/postgresql/old && mv /var/log/postgresql/raiden-* /var/log/postgresql/old/ 2>/dev/null; true")
    sh("systemctl restart postgresql@18-main")
    for _ in range(60):
        if pg_ready():
            break
        time.sleep(1)
    sh("mkdir -p /var/lib/arena/old && for f in ledger.jsonl stats.json timeline.jsonl; do [ -f /var/lib/arena/$f ] && mv /var/lib/arena/$f /var/lib/arena/old/$f.$(date +%s); done; true")
    sh("runuser -u arena -- psql -X -d ledger -c 'DROP TABLE IF EXISTS entries'")
    r = as_user("arena", PY, "/opt/arena/ledger.py", "init")
    mark("schema", out=(r.stdout + r.stderr).strip())
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
        for line in (ARENA / "timeline.jsonl").read_text().splitlines():
            d = json.loads(line)
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
    ap.add_argument("--rounds", default="crash,toast,pitr,bomb,boss2")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--warmup", type=int, default=60)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.jsonl").write_text("")
    t_start = time.time()
    mark("battle_start", rounds=args.rounds)
    if args.reset:
        reset_state()
    ensure_atomic_archive()
    start_workload()
    time.sleep(args.warmup)
    fresh_base_backup()
    before = snapshot_tree()
    sh("systemctl start raiden")
    time.sleep(15)
    mark("raiden_on_duty", ready=pg_ready(), baseline=reconcile())
    results = []
    for key in args.rounds.split(","):
        results.append(run_bomb_round(key.strip()) if ROUNDS[key.strip()].get("bomb") else run_round(key.strip()))
        collect(t_start, {"in_progress": True, "rounds": results})
        if results[-1].get("invalid"):
            mark("battle_aborted", reason=results[-1]["invalid"])
            stop_workload()
            return 2
        time.sleep(30)
    stats = stop_workload()
    final = reconcile(amcheck=True)
    after = snapshot_tree()
    changed = sorted(p.replace("/opt/raiden/", "") for p, h in after.items() if before.get(p) != h)
    payload = {
        "started_at": iso(t_start), "finished_at": iso(), "duration_s": int(time.time() - t_start),
        "rounds": results, "workload": stats, "final": final,
        "flawless": bool(final.get("ok")) and final.get("lost") == 0 and all(r["won"] for r in results),
        "rounds_won": sum(1 for r in results if r["won"]), "rounds_total": len(results),
        "artifacts_added_or_changed": changed,
        "model": os.environ.get("RAIDEN_AOAI_DEPLOYMENT", "gpt-5.5"),
    }
    mark("battle_end", flawless=payload["flawless"], final=final)
    collect(t_start, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
