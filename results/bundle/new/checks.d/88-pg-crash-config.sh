#!/usr/bin/env bash
# 88-pg-crash-config.sh - detects pg_crash preload/custom crash.* settings left in planned PostgreSQL config.
# On WARN/CRIT use skill: learned-pg-crash-preload. Added after incident inc-20261020T063807-pat.
set -euo pipefail
NAME=pg-crash-config
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-3}
POSTGRES_BIN=/usr/lib/postgresql/18/bin/postgres
PGDATA_DIR=/var/lib/postgresql/18/main
CONFIG_FILE=/etc/postgresql/18/main/postgresql.conf
EXTRA_CONFIG_FILES=${RAIDEN_PG_CRASH_EXTRA_CONFIG_FILES:-}

one_line() { tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | cut -c1-220; }

if ! disk_matches="$(timeout 10 sudo -n -u postgres RAIDEN_PG_CRASH_EXTRA_CONFIG_FILES="$EXTRA_CONFIG_FILES" bash -c '
set -euo pipefail
files=(/etc/postgresql/18/main/postgresql.conf)
for f in /etc/postgresql/18/main/conf.d/*.conf; do
  [ -e "$f" ] && files+=("$f")
done
if [ -n "${RAIDEN_PG_CRASH_EXTRA_CONFIG_FILES:-}" ]; then
  # shellcheck disable=SC2206
  extra=( ${RAIDEN_PG_CRASH_EXTRA_CONFIG_FILES} )
  for f in "${extra[@]}"; do
    [ -e "$f" ] && files+=("$f")
  done
fi
grep -HnE "^[[:space:]]*[^#].*(pg_crash|crash[.][A-Za-z0-9_]+[[:space:]]*=)" "${files[@]}" 2>/dev/null || true
' 2>&1)"; then
  echo "WARN $NAME: cannot scan PostgreSQL config files: $(one_line <<<"$disk_matches")"
  exit 1
fi
if [ -n "$disk_matches" ]; then
  echo "CRIT $NAME: pg_crash/crash.* present in config files: $(one_line <<<"$disk_matches")"
  exit 2
fi

planned_cmd=(sudo -n -u postgres "$POSTGRES_BIN" -D "$PGDATA_DIR" -c "config_file=$CONFIG_FILE" -C shared_preload_libraries)
if ! planned="$(timeout 10 "${planned_cmd[@]}" 2>&1)"; then
  echo "WARN $NAME: cannot read planned shared_preload_libraries: $(one_line <<<"$planned")"
  exit 1
fi
if grep -qi 'pg_crash' <<<"$planned"; then
  echo "CRIT $NAME: planned shared_preload_libraries contains pg_crash: $(one_line <<<"$planned")"
  exit 2
fi

q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "$1"; }
if ! file_settings="$(q "SELECT coalesce(string_agg(sourcefile || ':' || sourceline || ' ' || name || '=' || setting, E'\\n' ORDER BY sourcefile,sourceline,name),'') FROM pg_file_settings WHERE name LIKE 'crash.%' OR name ILIKE '%pg_crash%' OR setting ILIKE '%pg_crash%';")"; then
  echo "WARN $NAME: database unreachable for pg_file_settings check"
  exit 1
fi
if [ -n "$file_settings" ]; then
  echo "CRIT $NAME: pg_file_settings contains pg_crash/crash.*: $(one_line <<<"$file_settings")"
  exit 2
fi

echo "OK $NAME: no pg_crash library or crash.* settings in planned/live config"
exit 0
