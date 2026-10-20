#!/usr/bin/env bash
# 20-wraparound.sh - transaction-ID and multixact wraparound headroom of all databases.
#
# Usage: 20-wraparound.sh          (no arguments, read-only, < 1 s; bounded to ~10 s)
# Thresholds on max(age(datfrozenxid)) and max(mxid_age(datminmxid)): >= 1.2e9 WARN, >= 1.7e9 CRIT.
# Last line: "OK|WARN|CRIT wraparound: <message>"; exit 0/1/2. On WARN/CRIT use skill: wraparound-vacuum.
set -euo pipefail
NAME=wraparound
WARN_AGE=1200000000
CRIT_AGE=1700000000
export PATH="/usr/lib/postgresql/18/bin:$PATH" PGCONNECT_TIMEOUT=3

if ! r="$(timeout 8 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "
    SELECT (SELECT datname || '|' || age(datfrozenxid) FROM pg_database ORDER BY age(datfrozenxid) DESC LIMIT 1),
           (SELECT datname || '|' || mxid_age(datminmxid) FROM pg_database ORDER BY mxid_age(datminmxid) DESC LIMIT 1)" 2>&1)"; then
  echo "WARN $NAME: database unreachable, check skipped (${r//$'\n'/ })"; exit 1
fi
IFS='|' read -r xdb xage mdb mage <<<"$r"
msg="max xid age $xage ($xdb), max multixact age $mage ($mdb)"
if [ "$xage" -ge "$CRIT_AGE" ] || [ "$mage" -ge "$CRIT_AGE" ]; then
  echo "CRIT $NAME: $msg >= $CRIT_AGE"; exit 2
fi
if [ "$xage" -ge "$WARN_AGE" ] || [ "$mage" -ge "$WARN_AGE" ]; then
  echo "WARN $NAME: $msg >= $WARN_AGE"; exit 1
fi
echo "OK $NAME: $msg"
exit 0
