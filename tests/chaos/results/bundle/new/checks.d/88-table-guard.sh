#!/usr/bin/env bash
# Read-only table/registry/DDL guard drift detector after inc-20261021T005838-7552.
# On WARN/CRIT use skill: learned-table-drop-guard.
set -euo pipefail
NAME=table-guard
export PGCONNECT_TIMEOUT=3
BASE=/var/lib/raiden/memory/table-guard.baseline
SQL=/opt/raiden/checks.d/88-table-guard.sql
if [[ ! -s "$BASE" || ! -s "$SQL" ]]; then
 echo "CRIT $NAME: independent baseline or query missing"; exit 2
fi
expected="$(cat "$BASE")"
if ! actual="$(timeout 10 psql -X -qAt -v ON_ERROR_STOP=1 -d ledger -f "$SQL" 2>&1)"; then
 if [[ "$actual" == *'could not connect'* || "$actual" == *'connection to server'* || "$actual" == *'database system is'* || "$actual" == *'timeout expired'* ]]; then
  echo "WARN $NAME: ledger unreachable; guard inspection skipped"; exit 1
 fi
 echo "CRIT $NAME: guard catalog query failed; inspect missing registry/function or permissions"; exit 2
fi
if [[ "$actual" != "$expected" ]]; then
 echo "CRIT $NAME: table/registry/trigger/function/owner/arena-privilege drift: '$actual', expected '$expected'"; exit 2
fi
echo "OK $NAME: entries exists and is registered; DROP guard fingerprint, owners and arena ACL intact"
