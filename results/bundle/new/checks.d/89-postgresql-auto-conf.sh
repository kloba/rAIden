#!/usr/bin/env bash
# 89-postgresql-auto-conf.sh - detects unsafe ALTER SYSTEM drift in postgresql.auto.conf before restart.
# On WARN/CRIT use skill: learned-pg-crash-preload. Added after incident inc-20261020T064521-82fd.
set -euo pipefail
NAME=postgresql-auto-conf
BASELINE=/var/lib/raiden/memory/preload-workers.baseline
AUTO_CONF=${RAIDEN_AUTO_CONF_FILE:-/var/lib/postgresql/18/main/postgresql.auto.conf}
POSTGRES_BIN=/usr/lib/postgresql/18/bin/postgres
PGDATA_DIR=/var/lib/postgresql/18/main
CONFIG_FILE=/etc/postgresql/18/main/postgresql.conf
export PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT:-3}

one_line() { tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | cut -c1-220; }

if [ ! -r "$BASELINE" ]; then
  echo "WARN $NAME: missing baseline $BASELINE"
  exit 1
fi
# shellcheck disable=SC1090
source "$BASELINE"
expected_preload="$(tr -d '[:space:]' <<<"${allowed_shared_preload_libraries:-}")"
if [ -z "$expected_preload" ]; then
  echo "WARN $NAME: incomplete baseline $BASELINE"
  exit 1
fi

if ! auto_text="$(timeout 10 sudo -n -u postgres cat "$AUTO_CONF" 2>&1)"; then
  echo "WARN $NAME: cannot read $AUTO_CONF: $(one_line <<<"$auto_text")"
  exit 1
fi

if unsafe_lines="$(grep -nE "^[[:space:]]*[^#].*(pg_crash|crash[.][A-Za-z0-9_]+[[:space:]]*=)" <<<"$auto_text" || true)"; [ -n "$unsafe_lines" ]; then
  echo "CRIT $NAME: unsafe ALTER SYSTEM setting in $AUTO_CONF: $(one_line <<<"$unsafe_lines")"
  exit 2
fi

auto_preload="$(awk -F= '
  /^[[:space:]]*shared_preload_libraries[[:space:]]*=/ {
    v=$0; sub(/^[^=]*=/,"",v); sub(/[[:space:]]*(#.*)?$/,"",v); gsub(/[[:space:]'"'"']/,"",v); print v
  }' <<<"$auto_text" | tail -1)"
if [ -n "$auto_preload" ] && [ "$auto_preload" != "$expected_preload" ]; then
  echo "CRIT $NAME: $AUTO_CONF sets shared_preload_libraries='$auto_preload', expected '$expected_preload'"
  exit 2
fi

planned_cmd=(sudo -n -u postgres "$POSTGRES_BIN" -D "$PGDATA_DIR" -c "config_file=$CONFIG_FILE" -C shared_preload_libraries)
if ! planned="$(timeout 10 "${planned_cmd[@]}" 2>&1)"; then
  echo "WARN $NAME: cannot read planned shared_preload_libraries: $(one_line <<<"$planned")"
  exit 1
fi
planned="$(tr -d '[:space:]' <<<"$planned")"
if [ "$planned" != "$expected_preload" ]; then
  echo "CRIT $NAME: planned shared_preload_libraries='$planned', expected '$expected_preload'"
  exit 2
fi

q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "$1"; }
sql="SELECT coalesce(string_agg(sourcefile || ':' || sourceline || ' ' || name || '=' || setting, E'\n' ORDER BY sourceline,name),'') FROM pg_file_settings WHERE sourcefile = '$AUTO_CONF' AND ((name = 'shared_preload_libraries' AND replace(setting,' ','') <> '$expected_preload') OR name LIKE 'crash.%' OR setting ILIKE '%pg_crash%');"
if ! bad_file_settings="$(q "$sql")"; then
  echo "WARN $NAME: database unreachable for pg_file_settings auto.conf check"
  exit 1
fi
if [ -n "$bad_file_settings" ]; then
  echo "CRIT $NAME: pg_file_settings has unsafe postgresql.auto.conf entries: $(one_line <<<"$bad_file_settings")"
  exit 2
fi

echo "OK $NAME: postgresql.auto.conf and planned shared_preload_libraries match baseline '$expected_preload'; no pg_crash/crash.* ALTER SYSTEM drift"
exit 0
