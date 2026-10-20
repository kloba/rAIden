#!/bin/bash
# ROUND 1: somebody leaves a "temporary" chaos experiment in conf.d and restarts PostgreSQL.
# pg_crash (CYBERTEC) adds a background worker that sends a random signal to a random PostgreSQL process.
set -euo pipefail
DELAY="${DELAY:-4}"
SIGNALS="${SIGNALS:-9 15 3}"
CONF=/etc/postgresql/18/main/conf.d/50-experiment.conf
printf '%s\n' \
  "# TEMPORARY - game day experiment, remove afterwards" \
  "shared_preload_libraries = 'pg_stat_statements,pg_crash'" \
  "crash.delay = ${DELAY}" \
  "crash.signals = '${SIGNALS}'" > "$CONF"
chown postgres:postgres "$CONF"
chmod 644 "$CONF"
systemctl restart postgresql@18-main
