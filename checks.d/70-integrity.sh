#!/usr/bin/env bash
# 70-integrity.sh - data checksum failures and invalid indexes.
#
# Usage: 70-integrity.sh           (no arguments, read-only, < 2 s; each query bounded to 5 s)
# CRIT: sum(pg_stat_database.checksum_failures) > 0.
# WARN: data_checksums off (baseline is on); any pg_index.indisvalid = false in a connectable database
#       (indexes being built right now, listed in pg_stat_progress_create_index, are ignored); DB unreachable.
# Last line: "OK|WARN|CRIT integrity: <message>"; exit 0/1/2.
# On CRIT use skill: toast-corruption; invalid index: REINDEX INDEX CONCURRENTLY (see health-patrol).
set -euo pipefail
NAME=integrity
export PATH="/usr/lib/postgresql/18/bin:$PATH" PGCONNECT_TIMEOUT=3
q() { timeout 5 psql -X -At -v ON_ERROR_STOP=1 -d "$1" -c "$2" 2>&1; }

if ! r="$(q postgres "SELECT coalesce(sum(checksum_failures), 0),
       coalesce(string_agg(coalesce(datname, '<shared>') || '=' || checksum_failures, ',') FILTER (WHERE checksum_failures > 0), ''),
       current_setting('data_checksums')
       FROM pg_stat_database")"; then
  echo "WARN $NAME: database unreachable, check skipped (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r failures per_db checksums <<<"$r"

if ! dbs="$(q postgres "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY 1")"; then
  dbs=""
fi
invalid="" errors=""
while read -r db; do
  [ -n "$db" ] || continue
  if out="$(q "$db" "SELECT string_agg(i.indexrelid::regclass::text, ',') FROM pg_index i
                   WHERE NOT i.indisvalid
                     AND i.indexrelid NOT IN (SELECT index_relid FROM pg_stat_progress_create_index WHERE index_relid IS NOT NULL)")"; then
    [ -n "$out" ] && invalid+=" $db:$out"
  else
    errors+=" $db"
  fi
done <<<"$dbs"

if [ "$failures" -gt 0 ]; then
  echo "CRIT $NAME: $failures data checksum failure(s) ($per_db) - corruption, see toast-corruption${invalid:+; invalid indexes:$invalid}"; exit 2
fi
warn=""
[ "$checksums" = on ] || warn+=" data_checksums=$checksums (expected on);"
[ -n "$invalid" ] && warn+=" invalid indexes:$invalid;"
[ -n "$errors" ] && warn+=" could not check databases:$errors;"
if [ -n "$warn" ]; then echo "WARN $NAME:$warn"; exit 1; fi
echo "OK $NAME: 0 checksum failures, data_checksums=on, no invalid indexes"
exit 0
