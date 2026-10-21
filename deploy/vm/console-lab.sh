#!/usr/bin/env bash
# The console's test lab on the Azure VM (never on a laptop): PostgreSQL 13 to 18 side by side from PGDG (13 only while
# the archive still offers it), a streaming standby of 17, a least-privilege monitoring login set up exactly as the
# onboarding page tells people to, pgbench data owned by a "migrator" role, the roles of the lock-queue scenario, a
# light background workload, and a Python environment for the console. Idempotent.
#   deploy/vm/run.sh -f deploy/vm/console-lab.sh
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
cd /  # sudo -u postgres cannot enter Run Command's working directory
LAB=/etc/raiden-lab
install -d -m 750 "$LAB" /var/lib/raiden-lab
[ -f "$LAB/password" ] || { openssl rand -hex 16 > "$LAB/password"; chmod 600 "$LAB/password"; }
PW="$(cat "$LAB/password")"
log() { printf '%s  %s\n' "$(date +%H:%M:%S)" "$*"; }

if [ ! -f /etc/apt/sources.list.d/pgdg.list ]; then
  log "PGDG repository"
  apt-get update -q >/dev/null
  apt-get install -y -q curl ca-certificates postgresql-common python3-venv >/dev/null
  install -d /usr/share/postgresql-common/pgdg
  curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt noble-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -q >/dev/null
fi
# one cluster per version on its own port, no default "main" clusters
grep -q '^create_main_cluster = false' /etc/postgresql-common/createcluster.conf || echo 'create_main_cluster = false' >> /etc/postgresql-common/createcluster.conf

VERSIONS=""
for v in 13 14 15 16 17 18; do
  if dpkg -s "postgresql-$v" >/dev/null 2>&1 || apt-get install -y -q "postgresql-$v" >/dev/null 2>&1; then VERSIONS="$VERSIONS $v"; else log "postgresql-$v is not offered here: skipped"; fi
done
log "PostgreSQL versions:$VERSIONS"

psql_() { sudo -u postgres psql -X -q -v ON_ERROR_STOP=1 -p "$1" "${@:2}"; }
SERVERS="["
for v in $VERSIONS; do
  port=$((5400 + v))
  if ! pg_lsclusters -h | awk '{print $1"/"$2}' | grep -qx "$v/lab"; then
    pg_createcluster "$v" lab -p "$port" -- --data-checksums >/dev/null
  fi
  conf="/etc/postgresql/$v/lab/conf.d/50-lab.conf"
  want="listen_addresses = '127.0.0.1'
max_prepared_transactions = 10
log_lock_waits = on"
  # 13 keeps the defaults: the console must cope with a server without pg_stat_statements
  if [ "$v" -ge 14 ]; then want="$want
shared_preload_libraries = 'pg_stat_statements'"; fi
  if [ "$v" = 17 ]; then want="$want
wal_level = logical"; fi
  # restart only when the configuration changed: a re-run must not cut the running workload
  if [ "$want" != "$(cat "$conf" 2>/dev/null)" ]; then
    printf '%s\n' "$want" > "$conf"
    pg_ctlcluster "$v" lab stop 2>/dev/null || true
  fi
  pg_ctlcluster "$v" lab status >/dev/null 2>&1 || pg_ctlcluster "$v" lab start
  psql_ "$port" <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raiden_monitor') THEN CREATE ROLE raiden_monitor LOGIN CONNECTION LIMIT 5; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lab_admin') THEN CREATE ROLE lab_admin LOGIN SUPERUSER; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN CREATE ROLE migrator LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst') THEN CREATE ROLE analyst LOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app') THEN CREATE ROLE app LOGIN; END IF;
END \$\$;
ALTER ROLE raiden_monitor PASSWORD '$PW';
ALTER ROLE lab_admin PASSWORD '$PW';
ALTER ROLE migrator PASSWORD '$PW';
ALTER ROLE analyst PASSWORD '$PW';
ALTER ROLE app PASSWORD '$PW';
-- the onboarding page's login: statistics, plus cancelling and ending sessions
GRANT pg_monitor TO raiden_monitor;
GRANT pg_signal_backend TO raiden_monitor;
SQL
  if [ "$v" -ge 17 ]; then psql_ "$port" -c "GRANT pg_maintain TO raiden_monitor"; fi
  if ! psql_ "$port" -Atc "SELECT 1 FROM pg_database WHERE datname = 'app'" | grep -q 1; then
    sudo -u postgres createdb -p "$port" -O migrator app
    PGPASSWORD="$PW" /usr/lib/postgresql/"$v"/bin/pgbench -q -h 127.0.0.1 -p "$port" -U migrator -i -s 10 app >/dev/null 2>&1
    psql_ "$port" -d app -c "GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app"
  fi
  if [ "$v" -ge 14 ]; then
    psql_ "$port" -d app -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
    psql_ "$port" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"
  fi
  # a light, steady workload so rates, top queries and table statistics are real
  if ! systemctl is-active --quiet "lab-pgbench-$v"; then
    systemctl reset-failed "lab-pgbench-$v" 2>/dev/null || true
    systemd-run --quiet --unit "lab-pgbench-$v" --setenv=PGPASSWORD="$PW" \
      /usr/lib/postgresql/"$v"/bin/pgbench -h 127.0.0.1 -p "$port" -U app -c 2 -R 15 -T 14400 -n app
  fi
  SERVERS="$SERVERS{\"name\": \"pg$v\", \"lab_password\": \"$PW\","
  SERVERS="$SERVERS \"monitor\": {\"host\": \"127.0.0.1\", \"port\": $port, \"dbname\": \"app\", \"user\": \"raiden_monitor\", \"password\": \"$PW\"},"
  SERVERS="$SERVERS \"admin\": {\"host\": \"127.0.0.1\", \"port\": $port, \"dbname\": \"app\", \"user\": \"lab_admin\", \"password\": \"$PW\"}},"
done

if echo " $VERSIONS " | grep -q " 17 "; then
  if ! pg_lsclusters -h | awk '{print $1"/"$2}' | grep -qx "17/standby"; then
    log "streaming standby of 17 on 5517"
    pg_createcluster 17 standby -p 5517 >/dev/null
    rm -rf /var/lib/postgresql/17/standby
    sudo -u postgres /usr/lib/postgresql/17/bin/pg_basebackup -d "host=127.0.0.1 port=5417 user=lab_admin password=$PW" \
      -D /var/lib/postgresql/17/standby -R -X stream -C -S lab_standby
    echo "listen_addresses = '127.0.0.1'" > /etc/postgresql/17/standby/conf.d/50-lab.conf
    echo "max_prepared_transactions = 10" >> /etc/postgresql/17/standby/conf.d/50-lab.conf
    pg_ctlcluster 17 standby start
  fi
  SERVERS="$SERVERS{\"name\": \"pg17-standby\", \"lab_password\": \"$PW\","
  SERVERS="$SERVERS \"monitor\": {\"host\": \"127.0.0.1\", \"port\": 5517, \"dbname\": \"app\", \"user\": \"raiden_monitor\", \"password\": \"$PW\"},"
  SERVERS="$SERVERS \"admin\": {\"host\": \"127.0.0.1\", \"port\": 5517, \"dbname\": \"app\", \"user\": \"lab_admin\", \"password\": \"$PW\"}},"
fi
printf '%s]\n' "${SERVERS%,}" > "$LAB/servers.json"
chmod 600 "$LAB/servers.json"

# the console's own state store, as in docker-compose.yml: a cluster of its own, not one of the monitored servers
if echo " $VERSIONS " | grep -q " 17 "; then
  if ! pg_lsclusters -h | awk '{print $1"/"$2}' | grep -qx "17/state"; then
    log "state store cluster 17/state on 5499"
    pg_createcluster 17 state -p 5499 >/dev/null
    echo "listen_addresses = '127.0.0.1'" > /etc/postgresql/17/state/conf.d/50-lab.conf
  fi
  pg_ctlcluster 17 state status >/dev/null 2>&1 || pg_ctlcluster 17 state start
  psql_ 5499 <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'raiden') THEN CREATE ROLE raiden LOGIN; END IF;
END \$\$;
ALTER ROLE raiden PASSWORD '$PW';
SQL
  psql_ 5499 -Atc "SELECT 1 FROM pg_database WHERE datname = 'raiden'" | grep -q 1 || sudo -u postgres createdb -p 5499 -O raiden raiden
  echo "postgresql+psycopg://raiden:$PW@127.0.0.1:5499/raiden" > "$LAB/state-db-url"
  chmod 600 "$LAB/state-db-url"
fi

if [ ! -x /opt/raiden-lab/venv/bin/python ]; then
  python3 -m venv /opt/raiden-lab/venv
  /opt/raiden-lab/venv/bin/pip install -q --upgrade pip >/dev/null
fi
pg_lsclusters
log "servers: $(python3 -c "import json; print([s['name'] for s in json.load(open('$LAB/servers.json'))])")"
