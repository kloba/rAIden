#!/usr/bin/env bash
# Read-only attempted/rejected exact entries DROP detector after inc-20261021T013448-3b98.
# On WARN/CRIT use learned-table-drop-guard; does not suppress rejected attacks.
set -euo pipefail
export PGCONNECT_TIMEOUT=3
if ! timeout 5 psql -X -qAt -v ON_ERROR_STOP=1 -d ledger -c 'SELECT 1' >/dev/null 2>&1; then
 echo 'WARN ddl-attempts: ledger unreachable; log correlation skipped'; exit 1
fi
set +e
out="$(timeout 5 /opt/raiden/venv/bin/python /opt/raiden/.claude/skills/learned-table-drop-guard/scripts/ddl_attempts.py 2>&1)"
rc=$?
set -e
if [[ "$rc" -gt 2 || "$out" != *'ddl-attempts:'* ]]; then
 echo 'WARN ddl-attempts: bounded log sampler failed; inspect manually'; exit 1
fi
printf '%s\n' "$out"
exit "$rc"
