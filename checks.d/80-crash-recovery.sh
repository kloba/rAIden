#!/usr/bin/env bash
# 80-crash-recovery.sh - crash / crash-recovery events in the PostgreSQL jsonlog during the last 30 minutes.
#
# Usage: 80-crash-recovery.sh [MINUTES]   (default 30; read-only, does not need the database; < 2 s)
# Counts jsonlog lines whose message contains "database system was not properly shut down" or
# "terminated by signal" in the two newest /var/log/postgresql/raiden-*.json files (covers midnight).
# Assumes log_timezone = UTC (timestamps are compared as "YYYY-MM-DD HH:MM:SS" strings).
# Thresholds: >= 1 WARN, >= 3 CRIT. No readable jsonlog => WARN.
# Last line: "OK|WARN|CRIT crash-recovery: <message>"; exit 0/1/2. On WARN/CRIT use skill: crash-loop-rca.
set -euo pipefail
NAME=crash-recovery
MIN="${1:-30}"
[[ "$MIN" =~ ^[0-9]+$ ]] || { echo "usage: $0 [MINUTES]" >&2; exit 64; }

mapfile -t LOGS < <(ls -1tr /var/log/postgresql/raiden-*.json 2>/dev/null | tail -n 2)
if [ "${#LOGS[@]}" -eq 0 ]; then echo "WARN $NAME: no /var/log/postgresql/raiden-*.json files found"; exit 1; fi
for f in "${LOGS[@]}"; do
  [ -r "$f" ] || { echo "WARN $NAME: cannot read $f"; exit 1; }
done

since="$(date -u -d "-${MIN} min" '+%F %T')"
if ! counts="$(timeout 10 grep -h -E 'not properly shut down|terminated by signal' "${LOGS[@]}" 2>/dev/null \
      | timeout 5 jq -Rr --arg s "$since" '
          fromjson? | select(.timestamp >= $s)
          | if (.message | test("not properly shut down")) then "recovery"
            elif (.message | test("terminated by signal")) then "signal" else empty end' \
      | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')"; then
  counts=""   # grep exits 1 when nothing matches: that is the healthy case
fi
recovery="$(sed -n 's/.*recovery=\([0-9]*\).*/\1/p' <<<"$counts")"; recovery="${recovery:-0}"
signal="$(sed -n 's/.*signal=\([0-9]*\).*/\1/p' <<<"$counts")"; signal="${signal:-0}"
total=$((recovery + signal))
last=""
if [ "$total" -gt 0 ]; then
  last="$(grep -h -E 'not properly shut down|terminated by signal' "${LOGS[@]}" 2>/dev/null | tail -n 1 \
          | jq -Rr 'fromjson? | "\(.timestamp) \(.message)"' 2>/dev/null | cut -c1-160)" || last=""
fi
msg="last ${MIN} min: $recovery crash-recovery start(s), $signal process(es) terminated by signal${last:+; last: $last}"
if [ "$total" -ge 3 ]; then echo "CRIT $NAME: $msg"; exit 2; fi
if [ "$total" -ge 1 ]; then echo "WARN $NAME: $msg"; exit 1; fi
echo "OK $NAME: $msg"
exit 0
