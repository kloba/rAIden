#!/usr/bin/env python3
"""The arena's application: a ledger that counts only ACKNOWLEDGED commits, and the oracle that checks them.

  ledger.py init                       create the schema (as the arena role)
  ledger.py run                        run the workload until SIGTERM / SIGINT
  ledger.py reconcile [--json]         compare the client-side ledger of acked commits with the database (read-only)
  ledger.py reconcile --amcheck        ... and run pg_amcheck over the ledger database

A commit counts as acknowledged only when COMMIT returned successfully to the client. If the connection dies
during COMMIT the outcome is unknown and the row is NOT recorded: the oracle never demands it and never forbids it.
FLAWLESS = every acknowledged commit is present, byte-identical and readable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

LEDGER = Path(os.environ.get("ARENA_LEDGER", "/var/lib/arena/ledger.jsonl"))
STATS = Path(os.environ.get("ARENA_STATS", "/var/lib/arena/stats.json"))
TIMELINE = Path(os.environ.get("ARENA_TIMELINE", "/var/lib/arena/timeline.jsonl"))
DSN = os.environ.get("ARENA_DSN", "dbname=ledger connect_timeout=3")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id          bigint PRIMARY KEY,
    account     int NOT NULL,
    amount      int NOT NULL,
    payload     text NOT NULL,
    payload_md5 text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE entries ALTER COLUMN payload SET STORAGE EXTERNAL;
CREATE INDEX IF NOT EXISTS entries_account_idx ON entries (account);
CREATE INDEX IF NOT EXISTS entries_created_idx ON entries (created_at);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(DSN, autocommit=False, prepare_threshold=None)


# ---------------------------------------------------------------------------------------------- workload
class AckLedger:
    """Append-only client-side record of acknowledged commits (what the application was promised)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.recs: list[dict] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    self.recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        self.fh = open(path, "a", buffering=1)

    def ack(self, rec: dict) -> None:
        with self.lock:
            self.fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            self.recs.append(rec)

    def sample(self) -> dict | None:
        with self.lock:
            if not self.recs:
                return None
            pool = self.recs[-300:] if random.random() < 0.7 else self.recs
            return random.choice(pool)

    def sync(self) -> None:
        with self.lock:
            self.fh.flush()
            os.fsync(self.fh.fileno())


class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.time()
        self.acked = self.connects = 0
        self.errors: dict[str, int] = {}
        self.reads: dict[str, int] = {}
        self.last_ack = time.time()
        self.max_gap = 0.0

    def ack(self) -> None:
        with self.lock:
            now = time.time()
            self.max_gap = max(self.max_gap, now - self.last_ack)
            self.last_ack = now
            self.acked += 1

    def error(self, exc: Exception) -> None:
        key = getattr(exc, "sqlstate", None) or exc.__class__.__name__
        with self.lock:
            self.errors[key] = self.errors.get(key, 0) + 1

    def read(self, outcome: str) -> None:
        with self.lock:
            self.reads[outcome] = self.reads.get(outcome, 0) + 1

    def connected(self) -> None:
        with self.lock:
            self.connects += 1

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "t": round(time.time(), 1), "started": round(self.started, 1), "acked": self.acked, "connects": self.connects,
                "errors": dict(self.errors), "reads": dict(self.reads), "since_last_ack_s": round(time.time() - self.last_ack, 1),
                "max_ack_gap_s": round(self.max_gap, 1),
            }


def writer(stop: threading.Event, ledger: AckLedger, stats: Stats, interval: float) -> None:
    conn = None
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            if conn is None or conn.closed:
                conn = connect()
                stats.connected()
            payload = os.urandom(random.randint(2048, 8192)).hex()
            rec = {
                "id": random.getrandbits(62), "acct": random.randint(1, 500), "amt": random.randint(-500, 500),
                "md5": hashlib.md5(payload.encode()).hexdigest(), "t": round(time.time(), 3),
            }
            conn.execute(
                "INSERT INTO entries (id, account, amount, payload, payload_md5) VALUES (%s, %s, %s, %s, %s)",
                (rec["id"], rec["acct"], rec["amt"], payload, rec["md5"]),
            )
            conn.commit()
        except Exception as exc:  # outcome unknown or failed: not acknowledged, not recorded
            stats.error(exc)
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
            stop.wait(random.uniform(0.2, 0.8))
            continue
        ledger.ack(rec)
        stats.ack()
        stop.wait(max(0.0, interval - (time.monotonic() - t0)))


def reader(stop: threading.Event, ledger: AckLedger, stats: Stats) -> None:
    """The application's report queries: they read rows back and notice damage."""
    conn = None
    while not stop.is_set():
        rec = ledger.sample()
        if rec is None:
            stop.wait(0.5)
            continue
        try:
            if conn is None or conn.closed:
                conn = connect()
            row = conn.execute("SELECT md5(payload) FROM entries WHERE id = %s", (rec["id"],)).fetchone()
            conn.rollback()
            stats.read("missing" if row is None else "ok" if row[0] == rec["md5"] else "corrupt")
        except Exception as exc:
            stats.read("error:" + (getattr(exc, "sqlstate", None) or exc.__class__.__name__))
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            conn = None
        stop.wait(0.1)


def flusher(stop: threading.Event, ledger: AckLedger, stats: Stats) -> None:
    prev = 0
    while not stop.wait(2.0):
        snap = stats.snapshot()
        ledger.sync()
        tmp = STATS.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap))
        tmp.replace(STATS)
        with open(TIMELINE, "a") as fh:
            fh.write(json.dumps({"t": snap["t"], "acked": snap["acked"], "delta": snap["acked"] - prev, "errors": sum(snap["errors"].values())}) + "\n")
        prev = snap["acked"]


def cmd_init(args: argparse.Namespace) -> int:
    with connect() as conn:
        conn.execute(SCHEMA)
    print("schema ready")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ledger, stats, stop = AckLedger(LEDGER), Stats(), threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    threads = [threading.Thread(target=writer, args=(stop, ledger, stats, args.interval), daemon=True) for _ in range(args.writers)]
    threads += [threading.Thread(target=reader, args=(stop, ledger, stats), daemon=True), threading.Thread(target=flusher, args=(stop, ledger, stats), daemon=True)]
    for t in threads:
        t.start()
    print(f"workload up: {args.writers} writers, ledger={LEDGER} ({len(ledger.recs)} acked rows already recorded)", flush=True)
    stop.wait()
    for t in threads:
        t.join(timeout=5)
    ledger.sync()
    print(json.dumps(stats.snapshot()))
    return 0


# ---------------------------------------------------------------------------------------------- oracle
def load_acked() -> dict[int, dict]:
    acked: dict[int, dict] = {}
    if LEDGER.exists():
        for line in LEDGER.read_text().splitlines():
            try:
                rec = json.loads(line)
                acked[rec["id"]] = rec
            except (json.JSONDecodeError, KeyError):
                pass
    return acked


FETCH = "SELECT id, md5(payload), payload_md5, account, amount FROM entries WHERE id = ANY(%s)"


def _fetch(conn: psycopg.Connection, ids: list[int]) -> list[tuple]:
    return conn.execute(FETCH, (ids,)).fetchall()


def reconcile(amcheck: bool = False) -> dict:
    t0 = time.time()
    acked = load_acked()
    out: dict = {"acked": len(acked), "ok": False}
    conn = None
    for attempt in range(10):  # the database may be mid-restart
        try:
            conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=None)
            break
        except psycopg.Error as exc:
            out["error"] = f"database unavailable: {str(exc).strip()[:120]}"
            time.sleep(1.5)
    if conn is None:
        return out
    out.pop("error", None)
    present: dict[int, tuple] = {}
    unreadable: list[int] = []
    ids = sorted(acked)
    step = 400
    for i in range(0, len(ids), step):
        chunk = ids[i : i + step]
        for attempt in range(6):
            try:
                if conn.closed:
                    conn = psycopg.connect(DSN, autocommit=True, prepare_threshold=None)
                rows = _fetch(conn, chunk)
                break
            except psycopg.errors.UndefinedTable:
                out["error"] = "table entries does not exist"
                return out
            except psycopg.errors.DataCorrupted:
                rows = []
                for one in chunk:  # find the damaged rows one by one
                    try:
                        rows += _fetch(conn, [one])
                    except psycopg.errors.DataCorrupted:
                        unreadable.append(one)
                    except psycopg.Error:
                        time.sleep(0.5)
                break
            except psycopg.Error as exc:
                if attempt == 5:
                    out["error"] = f"query failed: {str(exc).strip()[:120]}"
                    return out
                time.sleep(1.5)
        for r in rows:
            present[r[0]] = r
    lost = [i for i in ids if i not in present and i not in set(unreadable)]
    corrupt = [i for i, (_, h, stored, acct, amt) in present.items() if h != acked[i]["md5"] or stored != acked[i]["md5"] or acct != acked[i]["acct"] or amt != acked[i]["amt"]]
    extra = conn.execute("SELECT count(*) FROM entries").fetchone()[0] - len(present) - len(unreadable)
    out.update(
        present=len(present), lost=len(lost), corrupt=len(corrupt), unreadable=len(unreadable), unrecorded_rows_in_db=max(extra, 0),
        lost_sample=lost[:5], corrupt_sample=corrupt[:5], unreadable_sample=unreadable[:5], seconds=round(time.time() - t0, 1),
    )
    out["ok"] = not lost and not corrupt and not unreadable
    if amcheck:
        r = subprocess.run(["pg_amcheck", "-d", "ledger", "--heapallindexed", "-j", "2"], capture_output=True, text=True, timeout=600)
        out["amcheck"] = "ok" if r.returncode == 0 else (r.stdout + r.stderr).strip()[:400]
        out["ok"] = out["ok"] and r.returncode == 0
    return out


def cmd_reconcile(args: argparse.Namespace) -> int:
    res = reconcile(args.amcheck)
    if args.json:
        print(json.dumps(res))
    else:
        verdict = "OK - every acknowledged commit is present, identical and readable" if res["ok"] else "NOT OK"
        print(verdict)
        for k, v in res.items():
            if k != "ok":
                print(f"  {k}: {v}")
    return 0 if res["ok"] else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    r = sub.add_parser("run")
    r.add_argument("--writers", type=int, default=4)
    r.add_argument("--interval", type=float, default=0.15, help="seconds between inserts per writer")
    r.set_defaults(fn=cmd_run)
    c = sub.add_parser("reconcile")
    c.add_argument("--json", action="store_true")
    c.add_argument("--amcheck", action="store_true")
    c.set_defaults(fn=cmd_reconcile)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
