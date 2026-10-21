#!/usr/bin/env bash
# Bootstrap the rAIden lab VM (Ubuntu 24.04): PostgreSQL 18 from PGDG, pg_crash (built, NOT enabled),
# OS users, JSON logging, WAL archiving (no base backup yet), and the rAIden runtime layout.
# Idempotent. Run as root on the VM:  sudo bash bootstrap.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
exec > >(tee -a /var/log/raiden-bootstrap.log) 2>&1
PGV=18
PGBIN="/usr/lib/postgresql/$PGV/bin"
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

log "apt prerequisites"
apt-get update -y -q
apt-get install -y -q --no-install-recommends ca-certificates curl gnupg lsb-release git build-essential \
  jq sysstat python3-venv python3-pip rsync unzip acl lsof psmisc

log "PGDG repository + PostgreSQL $PGV"
install -d /usr/share/postgresql-common/pgdg
curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  > /etc/apt/sources.list.d/pgdg.list
apt-get update -y -q
apt-get install -y -q "postgresql-$PGV" "postgresql-client-$PGV" "postgresql-server-dev-$PGV"

log "OS users and groups"
id raiden >/dev/null 2>&1 || useradd -m -d /var/lib/raiden -s /bin/bash raiden
id arena  >/dev/null 2>&1 || useradd -m -d /var/lib/arena  -s /bin/bash arena
# the lab's troublemakers: OS accounts that map to login roles of the same name (peer authentication). The attacks in
# tests/chaos/scenarios run as these, so what rAIden sees in the logs and in pg_stat_activity is a named role, like in real life
for u in reporting analyst migrator; do
  id "$u" >/dev/null 2>&1 || useradd -r -M -d /nonexistent -s /usr/sbin/nologin "$u"
done
usermod -aG postgres,adm,arena raiden
chmod 750 /var/lib/arena
install -d -o raiden -g raiden -m 750 /var/lib/raiden/{incidents,memory,escalations}
# The PITR clone is created and started by the postgres OS user inside /var/lib/raiden/work,
# so postgres must be able to traverse /var/lib/raiden and write to work/.
chmod 751 /var/lib/raiden
install -d -o raiden -g postgres -m 2770 /var/lib/raiden/work
install -d -o root -g root -m 700 /var/lib/arena-private

log "PostgreSQL directories (WAL archive, base backups)"
install -d -o postgres -g postgres -m 750 /var/lib/postgresql/wal_archive
install -d -o postgres -g postgres -m 750 /var/backups/postgresql /var/backups/postgresql/base

log "baseline configuration (conf.d/10-raiden-baseline.conf)"
CONFD="/etc/postgresql/$PGV/main/conf.d"
install -d -o postgres -g postgres "$CONFD"
cat > "$CONFD/10-raiden-baseline.conf" <<'EOF'
# rAIden lab baseline. Later files in conf.d win.
listen_addresses = 'localhost'
max_connections = 100
shared_buffers = '1GB'
shared_preload_libraries = 'pg_stat_statements'
wal_level = replica
archive_mode = on
archive_command = 'test ! -f /var/lib/postgresql/wal_archive/%f && cp %p /var/lib/postgresql/wal_archive/%f'
archive_timeout = 60
restart_after_crash = on
track_io_timing = on
logging_collector = on
log_destination = 'jsonlog'
log_directory = '/var/log/postgresql'
log_filename = 'raiden-%Y%m%d.log'
log_file_mode = 0640
log_rotation_age = 1d
log_min_messages = warning
log_min_error_statement = error
log_statement = 'ddl'
log_checkpoints = on
log_lock_waits = on
log_temp_files = 0
log_autovacuum_min_duration = 1s
EOF
chown postgres:postgres "$CONFD/10-raiden-baseline.conf"
grep -q "^include_dir *= *'conf.d'" "/etc/postgresql/$PGV/main/postgresql.conf" \
  || echo "include_dir = 'conf.d'" >> "/etc/postgresql/$PGV/main/postgresql.conf"

log "restart PostgreSQL with the baseline"
systemctl restart "postgresql@$PGV-main"
for _ in $(seq 1 60); do pg_isready -q && break; sleep 1; done
pg_isready

log "roles, database, extensions"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raiden') THEN CREATE ROLE raiden LOGIN SUPERUSER; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arena')  THEN CREATE ROLE arena LOGIN; END IF;
  -- reporting: a report job that leaks connections; analyst: an analyst's forgotten transaction; migrator: the deploy
  -- tool that runs ALTER TABLE (it may, because it is a member of the table owner)
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporting') THEN CREATE ROLE reporting LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst')   THEN CREATE ROLE analyst LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator')  THEN CREATE ROLE migrator LOGIN IN ROLE arena; END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
SQL
sudo -u postgres psql -X -tAc "SELECT 1 FROM pg_database WHERE datname='ledger'" | grep -q 1 \
  || sudo -u postgres createdb -O arena ledger
sudo -u postgres psql -X -d ledger -c "CREATE EXTENSION IF NOT EXISTS amcheck"
sudo -u postgres psql -X -tAc "SHOW data_checksums"

log "PostgreSQL tools on PATH for sudo -u postgres (pg_resetwal is deliberately NOT linked)"
for b in pg_waldump pg_ctl pg_controldata pg_archivecleanup pg_checksums pg_config pg_verifybackup pg_amcheck pg_basebackup pg_dump pg_restore pg_isready; do
  [ -x "$PGBIN/$b" ] && [ ! -e "/usr/local/bin/$b" ] && ln -s "$PGBIN/$b" "/usr/local/bin/$b" || true
done

log "sudoers for the rAIden OS user"
cat > /etc/sudoers.d/raiden <<EOF
Defaults:raiden !requiretty
raiden ALL=(postgres) NOPASSWD: ALL
raiden ALL=(root) NOPASSWD: /usr/bin/systemctl start postgresql@$PGV-main, /usr/bin/systemctl stop postgresql@$PGV-main, /usr/bin/systemctl restart postgresql@$PGV-main, /usr/bin/systemctl reload postgresql@$PGV-main, /usr/bin/systemctl status postgresql@$PGV-main, /usr/bin/systemctl status postgresql@$PGV-main *, /usr/bin/journalctl, /usr/bin/journalctl *, /usr/bin/dmesg, /usr/bin/dmesg *
# the streaming replica that rAIden builds in its fortify session runs as its own unit (pg-standby.service)
raiden ALL=(root) NOPASSWD: /usr/bin/systemctl start pg-standby, /usr/bin/systemctl stop pg-standby, /usr/bin/systemctl restart pg-standby, /usr/bin/systemctl reload pg-standby, /usr/bin/systemctl status pg-standby, /usr/bin/systemctl start pg-standby.service, /usr/bin/systemctl stop pg-standby.service, /usr/bin/systemctl restart pg-standby.service, /usr/bin/systemctl reload pg-standby.service, /usr/bin/systemctl status pg-standby.service
EOF
chmod 440 /etc/sudoers.d/raiden
visudo -cf /etc/sudoers.d/raiden

log "build pg_crash (installed into the server lib dir, NOT enabled)"
PKGLIB="$($PGBIN/pg_config --pkglibdir)"
if [ ! -f "$PKGLIB/pg_crash.so" ]; then
  rm -rf /usr/local/src/pg_crash
  git clone --depth 1 https://github.com/cybertec-postgresql/pg_crash /usr/local/src/pg_crash
  make -C /usr/local/src/pg_crash PG_CONFIG="$PGBIN/pg_config"
  make -C /usr/local/src/pg_crash PG_CONFIG="$PGBIN/pg_config" install
fi
ls -l "$PKGLIB/pg_crash.so"

log "rAIden runtime layout"
install -d -o root -g root -m 755 /opt/raiden /opt/arena /etc/raiden
install -d -o raiden -g raiden -m 755 /opt/raiden/.claude /opt/raiden/.claude/skills /opt/raiden/checks.d
if [ ! -x /opt/raiden/venv/bin/python ]; then
  python3 -m venv /opt/raiden/venv
fi
/opt/raiden/venv/bin/pip install -q --upgrade pip
/opt/raiden/venv/bin/pip install -q openai azure-identity "psycopg[binary]" PyYAML pytest
/opt/raiden/venv/bin/pip list 2>/dev/null | grep -i -E "openai|azure-identity|psycopg|pyyaml|pytest"

# Deliberately NO base backup here: the lab starts like many real servers (WAL archiving on, zero base backups,
# so point-in-time recovery is impossible). rAIden's fortify session takes the first verified backup.
sudo -u postgres psql -X -c "SELECT pg_switch_wal()" >/dev/null
sleep 3
sudo -u postgres psql -X -tAc "SELECT 'archived='||archived_count||' failed='||failed_count FROM pg_stat_archiver"

echo "BOOTSTRAP_DONE $(date -Is)" > /var/lib/raiden/.bootstrap-done
log "bootstrap complete"
