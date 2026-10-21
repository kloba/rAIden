#!/usr/bin/env bash
# inc-597d: planned/live preload, masked experiments, module/worker drift.
# On CRIT use learned-preload-guard. Read-only; no restart or suppression.
set -euo pipefail
NAME=preload-guard
export PGCONNECT_TIMEOUT=3
if ! state=$(timeout 4 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "SELECT current_setting('shared_preload_libraries')='pg_stat_statements' AND current_setting('allow_alter_system')='off' AND NOT EXISTS (SELECT 1 FROM pg_settings WHERE name='shared_preload_libraries' AND pending_restart) AND NOT EXISTS (SELECT 1 FROM pg_file_settings WHERE error IS NOT NULL OR (name='shared_preload_libraries' AND setting <> 'pg_stat_statements') OR name LIKE 'crash.%') AND NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE backend_type NOT IN ('client backend','autovacuum launcher','autovacuum worker','background writer','checkpointer','walwriter','archiver','io worker','logical replication launcher','parallel worker','walsender','walsummarizer','walreceiver','startup'));" 2>/dev/null); then
  echo "WARN $NAME: database unreachable or metadata query failed"; exit 1
fi
if [ "$state" != t ]; then echo "CRIT $NAME: live preload/ALTER SYSTEM/config errors/masked experiment/worker drift; inspect pg_file_settings and pg_stat_activity"; exit 2; fi
if ! /usr/bin/sha256sum --status -c /var/lib/raiden/memory/preload-guard.baseline; then
  echo "CRIT $NAME: pinned configuration fingerprint changed or missing"; exit 2
fi
if ! planned=$(timeout 3 sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /etc/postgresql/18/main -C shared_preload_libraries 2>/dev/null); then
  echo "CRIT $NAME: planned startup config cannot be parsed"; exit 2
fi
if [ "$planned" != pg_stat_statements ]; then echo "CRIT $NAME: next startup preload '$planned' differs from approved pg_stat_statements"; exit 2; fi
if ! modules=$(timeout 3 sudo -n -u postgres grep -oE '/usr/lib/postgresql/18/lib/[^ ]+\.so' "/proc/$(cat /run/postgresql/18-main.pid)/maps" 2>/dev/null | sort -u); then
  echo "WARN $NAME: cannot inspect postmaster mapped modules"; exit 1
fi
if [ "$modules" != /usr/lib/postgresql/18/lib/pg_stat_statements.so ]; then
  echo "CRIT $NAME: mapped server module drift '$modules'"; exit 2
fi
echo "OK $NAME: approved pin, live/planned pg_stat_statements, no masked experiment or unknown workers/modules"
