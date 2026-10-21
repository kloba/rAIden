#!/bin/bash
# ALTER UPPERCUT: pg_crash comes back through ALTER SYSTEM. postgresql.auto.conf is read LAST, so it overrides every file in
# conf.d. shared_preload_libraries is a list: one literal per item ('a,b' would be stored as ONE library name and the server
# would not start). If the server refuses ALTER SYSTEM (allow_alter_system = off) the attacker gives up and reports why;
# a real restart only follows a change that was really accepted.
set -uo pipefail
out="$(sudo -u postgres psql -X -v ON_ERROR_STOP=1 2>&1 <<'SQL'
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements', 'pg_crash';
ALTER SYSTEM SET crash.delay = 4;
ALTER SYSTEM SET crash.signals = '9 15 3';
SQL
)"
rc=$?
if [ "$rc" -ne 0 ]; then
  echo "ATTACK_BLOCKED: $(printf '%s\n' "$out" | grep -m1 -E 'ERROR|FATAL' | sed -E 's/^(ERROR|FATAL):[[:space:]]+//')"
  exit 0
fi
systemctl restart postgresql@18-main
echo "ALTER SYSTEM accepted, restarted"
