#!/bin/bash
# REMATCH: THE TIME BOMB. The same "temporary" experiment file comes back, somebody reloads the configuration to
# "test" it (shared_preload_libraries needs a restart, so PostgreSQL only logs that it cannot apply it), and the
# restart that activates it is scheduled for the maintenance window, FUSE seconds from now.
# Nothing is broken yet: only a defender that looks at what the NEXT restart will load can stop it.
set -euo pipefail
FUSE="${FUSE:-300}"
CONF=/etc/postgresql/18/main/conf.d/50-experiment.conf
printf '%s\n' \
  "# TEMPORARY - game day experiment, remove afterwards" \
  "shared_preload_libraries = 'pg_stat_statements,pg_crash'" \
  "crash.delay = 4" \
  "crash.signals = '9 15 3'" > "$CONF"
chown postgres:postgres "$CONF"
chmod 644 "$CONF"
systemctl stop arena-maintenance-restart.timer arena-maintenance-restart.service 2>/dev/null || true
systemctl reset-failed arena-maintenance-restart.timer arena-maintenance-restart.service 2>/dev/null || true
sudo -u postgres psql -X -qAt -c "SELECT pg_reload_conf()" >/dev/null
systemd-run --quiet --unit=arena-maintenance-restart --on-active="${FUSE}s" /bin/systemctl restart postgresql@18-main
echo "bomb armed: restart in ${FUSE}s"
