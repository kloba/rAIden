#!/usr/bin/env bash
# On trouble use skill learned-replica-ops. Fortify fort-20261021T004657.
set -euo pipefail
export PGCONNECT_TIMEOUT=3 PGOPTIONS='-c statement_timeout=4000'
if ! v=$(timeout 6 psql -X -At -F '|' -v ON_ERROR_STOP=1 -d postgres -c "SELECT extract(epoch FROM now()-pg_postmaster_start_time())::bigint, coalesce(s.active,false), coalesce(r.state,'absent'), coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(),r.replay_lsn),-1)::bigint FROM (SELECT 1) x LEFT JOIN pg_replication_slots s ON s.slot_name='raiden_replica' LEFT JOIN pg_stat_replication r ON r.pid=s.active_pid"); then
  echo 'WARN replica: primary unreachable; use learned-replica-ops'; exit 1
fi
IFS='|' read -r uptime active state lag <<< "$v"
if (( uptime < 90 )) && { [[ $active != t || $state != streaming ]] || (( lag < 0 )); }; then
  echo 'OK replica: reconnecting within primary startup grace'; exit 0
fi
if [[ $active != t || $state != streaming ]]; then
  if [[ $active != t ]] && (( uptime > 300 )); then
    echo "CRIT replica: slot inactive, primary uptime=${uptime}s; use learned-replica-ops"; exit 2
  fi
  echo "WARN replica: active=$active state=$state; use learned-replica-ops"; exit 1
fi
if (( lag < 0 || lag >= 67108864 )); then
  echo "WARN replica: replay lag=${lag} bytes; use learned-replica-ops"; exit 1
fi
echo "OK replica: slot active, streaming, replay lag=${lag} bytes"; exit 0
