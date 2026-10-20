#!/bin/bash
# BOSS RETURNS: pg_crash comes back with a new move. ALTER SYSTEM writes postgresql.auto.conf,
# which PostgreSQL reads LAST - it overrides every file in conf.d.
# shared_preload_libraries is a list: one literal per item ('a,b' would be stored as ONE library name and the server would not start).
set -euo pipefail
sudo -u postgres psql -X -v ON_ERROR_STOP=1 <<'SQL'
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements', 'pg_crash';
ALTER SYSTEM SET crash.delay = 4;
ALTER SYSTEM SET crash.signals = '9 15 3';
SQL
systemctl restart postgresql@18-main
