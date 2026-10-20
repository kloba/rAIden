#!/usr/bin/env bash
# 60-sessions.sh - connection usage and the oldest idle-in-transaction session.
#
# Usage: 60-sessions.sh            (no arguments, read-only, < 1 s; bounded to ~10 s)
# Thresholds: client backends / max_connections >= 80% WARN, >= 95% CRIT;
#             oldest "idle in transaction" > 5 min WARN, > 30 min CRIT;
#             connection refused with "too many clients" / reserved-slot error => CRIT.
# Last line: "OK|WARN|CRIT sessions: <message>"; exit 0/1/2. On WARN/CRIT use skill: connection-storm.
set -euo pipefail
NAME=sessions
export PATH="/usr/lib/postgresql/18/bin:$PATH" PGCONNECT_TIMEOUT=3

if ! r="$(timeout 8 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
    SELECT count(*) FILTER (WHERE backend_type = 'client backend'),
           current_setting('max_connections')::int,
           coalesce(max(extract(epoch FROM now() - state_change))
                    FILTER (WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')), 0)::bigint,
           count(*) FILTER (WHERE state IN ('idle in transaction', 'idle in transaction (aborted)'))
    FROM pg_stat_activity" 2>&1)"; then
  case "$r" in
    *"too many clients"*|*"connection slots are reserved"*)
      echo "CRIT $NAME: connections exhausted: ${r//$'\n'/ }"; exit 2 ;;
  esac
  echo "WARN $NAME: database unreachable, check skipped (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r used max idle_tx_age idle_tx_n <<<"$r"
pct=$(( used * 100 / max ))
msg="$used/$max client connections (${pct}%), $idle_tx_n idle in transaction, oldest ${idle_tx_age}s"
if [ "$pct" -ge 95 ] || [ "$idle_tx_age" -gt 1800 ]; then echo "CRIT $NAME: $msg"; exit 2; fi
if [ "$pct" -ge 80 ] || [ "$idle_tx_age" -gt 300 ]; then echo "WARN $NAME: $msg"; exit 1; fi
echo "OK $NAME: $msg"
exit 0
