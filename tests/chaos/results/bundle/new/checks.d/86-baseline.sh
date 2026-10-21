#!/usr/bin/env bash
# Reloadable baseline and age of EVERY base backup; fort-20261021T004657.
set -euo pipefail
export PGCONNECT_TIMEOUT=3 PGOPTIONS='-c statement_timeout=4000'
if ! v=$(timeout 6 psql -X -At -F '|' -v ON_ERROR_STOP=1 -d postgres -c "SELECT current_setting('allow_alter_system'), current_setting('max_slot_wal_keep_size'), current_setting('idle_in_transaction_session_timeout'), (SELECT count(*) FROM pg_file_settings WHERE error IS NOT NULL), (SELECT count(*) FROM pg_file_settings WHERE applied AND sourcefile='/etc/postgresql/18/main/conf.d/98-raiden-baseline.conf' AND name IN ('allow_alter_system','max_slot_wal_keep_size','idle_in_transaction_session_timeout'))"); then
  echo 'WARN baseline: database unreachable'; exit 1
fi
IFS='|' read -r alter cap idle errors applied <<< "$v"
if [[ ! -f /etc/postgresql/18/main/conf.d/98-raiden-baseline.conf || $alter != off || $cap != 4GB || $idle != 5min || $errors != 0 || $applied != 3 ]]; then
  echo "CRIT baseline: alter=$alter cap=$cap idle=$idle config_errors=$errors applied=$applied"; exit 2
fi
if ! backups=$(timeout 4 sudo -n -u postgres ls -1 /var/backups/postgresql/base/); then
  echo 'WARN baseline: cannot list base backups'; exit 1
fi
if [[ -z $backups ]]; then echo 'WARN baseline: no base backups'; exit 1; fi
now=$(date -u +%s); count=0; old=0
while IFS= read -r ts; do
  if [[ ! $ts =~ ^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})$ ]]; then
    echo "WARN baseline: unrecognized backup name $ts"; exit 1
  fi
  if ! epoch=$(date -u -d "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} ${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:${BASH_REMATCH[6]}" +%s); then
    echo "WARN baseline: invalid backup timestamp $ts"; exit 1
  fi
  count=$((count+1))
  if (( now-epoch >= 604800 || epoch > now )); then old=$((old+1)); fi
done <<< "$backups"
if (( old > 0 )); then echo "WARN baseline: $old/$count backups stale (>7 days) or future-dated"; exit 1; fi
echo "OK baseline: ALTER SYSTEM off, 4GB slot cap, 5min idle timeout, config clean, all $count backups younger than 7 days"; exit 0
