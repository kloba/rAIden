#!/bin/bash
# ICE LOCK: the classic lock queue. An analyst opens a transaction, reads a few rows and goes to lunch ("idle in transaction",
# holding a plain read lock on the ledger table). Then the deploy tool (role "migrator", a member of the application role) runs
# its migration: LOCK TABLE ... ACCESS EXCLUSIVE, then ALTER TABLE ... ADD COLUMN. The lock request waits for the analyst, and
# every new INSERT of the application queues behind the waiting request. Nothing is broken, nothing is written, the
# application is simply frozen. (The LOCK needs only DML privileges, so the freeze does not depend on who owns the table.)
# If the freeze is cut short within 25 s (a lock_timeout for the deploy role, an idle-in-transaction timeout for the analyst,
# no privileges for the deploy role) the attacker reports how it ended and gives up.
set -uo pipefail
PY=/opt/raiden/venv/bin/python
D="$(mktemp -d /tmp/ice.XXXXXX)"; chmod 755 "$D"

setsid nohup runuser -u analyst -- "$PY" - > "$D/analyst.out" 2>&1 <<'PY' &
import time

import psycopg

c = psycopg.connect("dbname=ledger host=/var/run/postgresql connect_timeout=3", autocommit=False)
c.execute("SELECT count(*) FROM public.entries WHERE id < 0").fetchone()
print("ANALYST_READY: transaction open, read lock held, now idle", flush=True)
time.sleep(900)
PY

for _ in $(seq 1 15); do
  grep -q ANALYST_READY "$D/analyst.out" && break
  sleep 1
done
if ! grep -q ANALYST_READY "$D/analyst.out"; then
  echo "ice: the analyst session did not start: $(tail -c 300 "$D/analyst.out")" >&2
  exit 1
fi

setsid nohup runuser -u migrator -- "$PY" - > "$D/migrator.out" 2>&1 <<'PY' &
import time

import psycopg

t0 = time.time()
c = psycopg.connect("dbname=ledger host=/var/run/postgresql connect_timeout=3", autocommit=False)
try:
    c.execute("LOCK TABLE public.entries IN ACCESS EXCLUSIVE MODE")
    c.execute("ALTER TABLE public.entries ADD COLUMN IF NOT EXISTS note text")
    c.commit()
    print(f"MIGRATION_DONE after {time.time() - t0:.1f}s", flush=True)
except Exception as exc:
    print(f"MIGRATION_FAILED after {time.time() - t0:.1f}s: {str(exc).strip().splitlines()[0]}", flush=True)
PY

for _ in $(seq 1 25); do
  if grep -q -E '^MIGRATION_' "$D/migrator.out"; then
    echo "ATTACK_BLOCKED: the freeze was cut short: $(grep -m1 -E '^MIGRATION_' "$D/migrator.out")"
    pkill -u analyst || true
    exit 0
  fi
  sleep 1
done
echo "ATTACK_LANDED: ICE_LOCK_ON: LOCK TABLE public.entries has waited 25 s behind an idle transaction of role analyst; every INSERT of the application is queued behind it"
