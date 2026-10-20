#!/usr/bin/env bash
# Compact status of a running battle (fits the 4 KB Run Command output limit).  deploy/vm/peek.sh [N-tool-lines]
N="${1:-14}"
HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/run.sh" "
cd /var/lib/raiden 2>/dev/null
echo '== battle'; tail -n 4 /var/lib/arena/battle/events.jsonl 2>/dev/null | cut -c1-200
cat /var/lib/arena/battle/status.json 2>/dev/null | cut -c1-260; echo
echo '== pg'; pg_isready -h /var/run/postgresql; psql -X -At -d postgres -c \"select 'preload='||current_setting('shared_preload_libraries')\" 2>&1 | head -1
echo '== workload'; cut -c1-200 /var/lib/arena/stats.json 2>/dev/null; echo
echo '== raiden (last $N tool calls)'; jq -r 'select(.type==\"agent_tool\" or (.type|test(\"incident|evolution|liveness|guard\"))) | (.t[11:19]) + \" \" + (if .type==\"agent_tool\" then (if .denied then \"DENIED \" else \"\" end) + .tool + \" \" + (.summary[:105]) else .type + \" \" + ((.id // .rule // \"\")|tostring) end)' events.jsonl 2>/dev/null | tail -n $N
" 2>&1 | grep -v '^Enable succeeded' | cut -c1-230
