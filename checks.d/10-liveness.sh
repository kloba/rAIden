#!/usr/bin/env bash
# 10-liveness.sh - is PostgreSQL 18/main accepting connections, answering SELECT 1 and not in recovery?
#
# Usage: 10-liveness.sh            (no arguments, read-only, ~1 s; bounded to ~13 s when the DB hangs)
# Last line: "OK|WARN|CRIT liveness: <message>"; exit 0 OK / 2 CRIT.  On CRIT use skill: pg-triage.
set -euo pipefail
NAME=liveness
export PATH="/usr/lib/postgresql/18/bin:$PATH" PGCONNECT_TIMEOUT=3

rc=0
out="$(timeout 5 pg_isready -t 3 2>&1)" || rc=$?
case "$rc" in
  0) ;;
  1) echo "CRIT $NAME: server rejects connections (starting up or crash recovery): $out"; exit 2 ;;
  2) echo "CRIT $NAME: no response from server: $out"; exit 2 ;;
  124) echo "CRIT $NAME: pg_isready timed out"; exit 2 ;;
  *) echo "CRIT $NAME: pg_isready rc=$rc: $out"; exit 2 ;;
esac

if ! r="$(timeout 8 psql -X -At -v ON_ERROR_STOP=1 -d postgres \
      -c "SELECT 1, pg_is_in_recovery(), round(extract(epoch FROM now() - pg_postmaster_start_time()))::bigint" 2>&1)"; then
  echo "CRIT $NAME: pg_isready ok but SELECT 1 failed: ${r//$'\n'/ }"; exit 2
fi
IFS='|' read -r one inrec up <<<"$r"
if [ "$one" != 1 ]; then echo "CRIT $NAME: unexpected answer to SELECT 1: $r"; exit 2; fi
if [ "$inrec" != f ]; then echo "CRIT $NAME: server is in recovery (pg_is_in_recovery = $inrec)"; exit 2; fi
echo "OK $NAME: accepting connections, SELECT 1 ok, not in recovery, postmaster up ${up}s"
exit 0
