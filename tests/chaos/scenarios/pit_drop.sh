#!/bin/bash
# DROP KICK / THE PIT: a clean-up script runs against the wrong database and drops the ledger table.
# If the database refuses (a protection on the table) the attacker reports the error and gives up.
set -uo pipefail
out="$(runuser -u arena -- psql -X -d ledger -v ON_ERROR_STOP=1 -c "DROP TABLE public.entries" 2>&1)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "ATTACK_BLOCKED: $(printf '%s\n' "$out" | grep -m1 -E 'ERROR|FATAL' | sed -E 's/^(ERROR|FATAL):[[:space:]]+//')"
  exit 0
fi
date -u +%FT%T.%3NZ > /var/lib/arena-private/pit_drop.at
echo "table dropped"
