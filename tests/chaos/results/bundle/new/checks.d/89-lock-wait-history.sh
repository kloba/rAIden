#!/usr/bin/env bash
# After inc-20261021T014327-184f: retain brief production waits missed by live sampling.
# On WARN/CRIT use learned-lock-queue. Read-only; whole check bounded <15s.
set -euo pipefail
export PGCONNECT_TIMEOUT=3
if ! timeout 4 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c 'SELECT 1' >/dev/null 2>&1; then
  echo 'WARN lock-wait-history: database unavailable; confirm service/live chain'; exit 1
fi
set +e
out="$(timeout 8 /usr/bin/python3 /opt/raiden/.claude/skills/learned-lock-queue/scripts/lock_wait_history.py 2>&1)"
rc=$?
set -e
if (( rc > 2 )) || [[ ! "$out" =~ ^(OK|WARN|CRIT)\ lock-wait-history: ]]; then
  echo 'WARN lock-wait-history: bounded log reader failed; inspect manually'; exit 1
fi
printf '%s\n' "$out"
exit "$rc"
