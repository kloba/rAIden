#!/usr/bin/env bash
# 86-pg-surgery-toast.sh - detect pg_surgery and unsafe pg_toast surgery. On CRIT use skill: learned-pg-surgery-toast. Added after incident inc-20261020T062423-f135.
set -euo pipefail
NAME=pg-surgery-toast
export PGCONNECT_TIMEOUT=3
BASE=/var/lib/raiden/memory/pg-surgery-toast.baseline

if [ ! -r "$BASE" ]; then
  echo "WARN $NAME: missing baseline $BASE"
  exit 1
fi

activated_after=""
expected_pg_surgery_installed="0"
while IFS='=' read -r k v; do
  case "$k" in
    activated_after) activated_after="$v" ;;
    expected_pg_surgery_installed) expected_pg_surgery_installed="$v" ;;
  esac
done < <(grep -E '^(activated_after|expected_pg_surgery_installed)=' "$BASE" || true)

if [ -z "$activated_after" ]; then
  echo "WARN $NAME: baseline lacks activated_after"
  exit 1
fi

q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c "$1"; }
installed=""
if ! installed="$(q "SELECT count(*) FROM pg_extension WHERE extname = 'pg_surgery'")"; then
  echo "WARN $NAME: database unreachable, cannot confirm pg_surgery is absent"
  exit 1
fi

if [ "$installed" != "$expected_pg_surgery_installed" ]; then
  echo "CRIT $NAME: pg_surgery installed=$installed expected=$expected_pg_surgery_installed"
  exit 2
fi

logfile="/var/log/postgresql/raiden-$(date -u +%Y%m%d).json"
recent=0
sample=""
if [ -r "$logfile" ]; then
  # Look only after activation to avoid re-alerting on the incident that created this detector.
  # Match pg_surgery use, heap_force_kill, or DML/DDL against pg_toast relations.
  if sample="$(timeout 10 jq -Rr --arg since "$activated_after" '
    fromjson?
    | select((.timestamp[0:19] // "") > $since)
    | (.message // .statement // "") as $m
    | select($m | test("pg_surgery|heap_force_kill|statement: (CREATE|DROP) EXTENSION[^\n]*pg_surgery|statement: (INSERT|UPDATE|DELETE|TRUNCATE|COPY|ALTER|DROP)[^\n]*pg_toast\\."; "i"))
    | "\(.timestamp) \(.user // "?")@\(.dbname // "?") \($m)"
  ' "$logfile" | head -3)"; then
    if [ -n "$sample" ]; then recent="$(printf '%s\n' "$sample" | wc -l)"; fi
  else
    echo "WARN $NAME: could not scan $logfile"
    exit 1
  fi
fi

if [ "$recent" -gt 0 ]; then
  echo "CRIT $NAME: unsafe pg_surgery/pg_toast activity after $activated_after: $(printf '%s' "$sample" | tr '\n' '; ' | cut -c1-240)"
  exit 2
fi

echo "OK $NAME: pg_surgery absent and no unsafe pg_toast/pg_surgery log entries after $activated_after"
exit 0
