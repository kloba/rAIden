#!/usr/bin/env bash
# 85-preload-workers.sh - detects unauthorized shared_preload_libraries and unexpected PostgreSQL background workers.
# On WARN/CRIT use skill: learned-pg-crash-preload. Added after incident inc-20261020T061839-d105.
set -euo pipefail
NAME=preload-workers
BASELINE=/var/lib/raiden/memory/preload-workers.baseline
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-3}

if [ ! -r "$BASELINE" ]; then
  echo "WARN $NAME: missing baseline $BASELINE"
  exit 1
fi
# shellcheck disable=SC1090
source "$BASELINE"
allowed_shared_preload_libraries=${allowed_shared_preload_libraries:-}
allowed_backend_types=${allowed_backend_types:-}
if [ -z "$allowed_shared_preload_libraries" ] || [ -z "$allowed_backend_types" ]; then
  echo "WARN $NAME: incomplete baseline $BASELINE"
  exit 1
fi

planned_cmd=(sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main -c config_file=/etc/postgresql/18/main/postgresql.conf -C shared_preload_libraries)
if ! planned="$(timeout 10 "${planned_cmd[@]}" 2>&1)"; then
  echo "WARN $NAME: cannot read planned shared_preload_libraries: $(tr '\n' ' ' <<<"$planned" | cut -c1-180)"
  exit 1
fi
planned="$(tr -d '[:space:]' <<<"$planned")"
expected_preload="$(tr -d '[:space:]' <<<"$allowed_shared_preload_libraries")"
if [ "$planned" != "$expected_preload" ]; then
  echo "CRIT $NAME: planned shared_preload_libraries='$planned', expected '$expected_preload'"
  exit 2
fi

q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "$1"; }
if ! live="$(q "SHOW shared_preload_libraries")"; then
  echo "WARN $NAME: database unreachable for live preload check"
  exit 1
fi
live="$(tr -d '[:space:]' <<<"$live")"
if [ "$live" != "$expected_preload" ]; then
  echo "CRIT $NAME: live shared_preload_libraries='$live', expected '$expected_preload' (restart may be pending or unauthorized library loaded)"
  exit 2
fi

if ! unknown="$(q "WITH allowed(t) AS (SELECT unnest(string_to_array('$allowed_backend_types', ','))) SELECT string_agg(DISTINCT backend_type, ', ' ORDER BY backend_type) FROM pg_stat_activity WHERE backend_type NOT IN (SELECT t FROM allowed)")"; then
  echo "WARN $NAME: database unreachable for background worker check"
  exit 1
fi
if [ -n "$unknown" ]; then
  echo "CRIT $NAME: unexpected backend_type(s): $unknown"
  exit 2
fi

echo "OK $NAME: planned/live shared_preload_libraries='$expected_preload'; backend types match baseline"
exit 0
