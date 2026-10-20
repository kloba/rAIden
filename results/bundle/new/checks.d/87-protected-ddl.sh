#!/usr/bin/env bash
# 87-protected-ddl.sh - detect missing protected tables and fresh destructive DDL. On CRIT use skill: learned-protected-table-drop-pitr. Added after incident inc-20261020T063221-0cee.
set -euo pipefail
NAME=protected-ddl
BASELINE=/var/lib/raiden/memory/protected-ddl.baseline
export PGCONNECT_TIMEOUT=3

if [ ! -r "$BASELINE" ]; then
  echo "WARN $NAME: missing baseline $BASELINE"
  exit 1
fi
# shellcheck source=/var/lib/raiden/memory/protected-ddl.baseline
. "$BASELINE"
: "${SINCE_UTC:?baseline must set SINCE_UTC}"
: "${PROTECTED_TABLES:?baseline must set PROTECTED_TABLES}"

q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d ledger -c "$1"; }

missing=""
for tbl in $PROTECTED_TABLES; do
  if ! exists="$(q "SELECT to_regclass('$tbl') IS NOT NULL")"; then
    echo "WARN $NAME: database unreachable, protected table check skipped"
    exit 1
  fi
  if [ "$exists" != "t" ]; then
    missing="$missing $tbl"
  fi
done
if [ -n "$missing" ]; then
  echo "CRIT $NAME: protected table(s) missing:$missing; use skill learned-protected-table-drop-pitr"
  exit 2
fi

logs="$(ls -1tr /var/log/postgresql/raiden-*.json 2>/dev/null | tail -2 || true)"
if [ -n "$logs" ]; then
  pattern=""
  for tbl in $PROTECTED_TABLES; do
    bare="${tbl##*.}"
    esc_tbl="$(printf '%s' "$tbl" | sed 's/[.[\\*^$()+?{}|]/\\&/g')"
    esc_bare="$(printf '%s' "$bare" | sed 's/[.[\\*^$()+?{}|]/\\&/g')"
    if [ -z "$pattern" ]; then
      pattern="($esc_tbl|\\b$esc_bare\\b)"
    else
      pattern="$pattern|($esc_tbl|\\b$esc_bare\\b)"
    fi
  done
  hit="$(jq -Rr --arg s "$SINCE_UTC" --arg p "$pattern" '
    fromjson?
    | select((.timestamp // "") >= $s)
    | select((.message // "") | test("^statement: *DROP TABLE|^statement: *TRUNCATE|^statement: *ALTER TABLE"; "i"))
    | select((.dbname // "") == "ledger")
    | select((.message // "") | test($p; "i"))
    | "\(.timestamp) \(.user // "-")@\(.dbname // "-") pid=\(.pid // "-") \(.message)"' $logs | head -1)"
  if [ -n "$hit" ]; then
    echo "CRIT $NAME: destructive DDL on protected table after $SINCE_UTC: $hit; use skill learned-protected-table-drop-pitr"
    exit 2
  fi
fi

echo "OK $NAME: protected tables present ($PROTECTED_TABLES); no destructive DDL since $SINCE_UTC"
exit 0
