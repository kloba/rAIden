#!/usr/bin/env bash
# replica_status.sh - one-screen health of the streaming replica (primary view + replica view).
# Usage: replica_status.sh [MAX_LAG_MB]   (default 64). Read-only, bounded to ~15 s.
# Exit 0 = streaming with small lag, 1 = replica missing / not streaming, 2 = lag above the limit.
set -uo pipefail
export PGCONNECT_TIMEOUT=3
MAXLAG_MB="${1:-64}"
rc=0
echo "== primary: replication slot and walsender"
timeout 10 psql -X -d postgres -c "select slot_name, active, wal_status, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as retained from pg_replication_slots where slot_name = 'raiden_replica'" || rc=1
lag="$(timeout 10 psql -X -At -d postgres -c "select coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)), -1)::bigint from pg_stat_replication where state = 'streaming'" 2>/dev/null || echo -1)"
timeout 10 psql -X -d postgres -c "select application_name, state, sync_state, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) as replay_lag from pg_stat_replication" || true
echo "== replica: unit and recovery state"
systemctl is-active pg-standby 2>/dev/null || true
if pg_isready -q -h /tmp -p 5434 -t 3; then
  timeout 10 psql -X -At -h /tmp -p 5434 -d postgres -c "select 'in_recovery='||pg_is_in_recovery()||' replayed_to='||pg_last_wal_replay_lsn()||' last_replay_age='||coalesce(now()-pg_last_xact_replay_timestamp(), interval '0')" || rc=1
else
  echo "replica does not accept connections on /tmp:5434"; rc=1
fi
if [ "$lag" = "-1" ]; then echo "VERDICT: no streaming walsender on the primary"; rc=1
elif [ "$lag" -gt $(( MAXLAG_MB * 1024 * 1024 )) ]; then echo "VERDICT: streaming but lag $((lag / 1024 / 1024)) MB > ${MAXLAG_MB} MB"; [ "$rc" -eq 0 ] && rc=2
elif [ "$rc" -eq 0 ]; then echo "VERDICT: healthy, streaming, lag ${lag} bytes"; fi
exit "$rc"
