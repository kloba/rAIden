#!/usr/bin/env bash
# Runs ON the VM (via Run Command): collect a compact, curated bundle of the battle results for the talk and the repo.
set -u
B=/var/lib/arena/deck-bundle
rm -rf "$B"; mkdir -p "$B/incidents" "$B/new"
BT=/var/lib/arena/battle
cp "$BT/battle.json" "$BT/timeline_10s.json" "$B/" 2>/dev/null
cp "$BT/events.jsonl" "$B/arena-events.jsonl" 2>/dev/null
for f in lessons.md evolution.md patrol.log; do cp "/var/lib/raiden/memory/$f" "$B/" 2>/dev/null; done
for d in /var/lib/raiden/incidents/*/; do
  id=$(basename "$d"); mkdir -p "$B/incidents/$id"
  cp "$d/incident.json" "$d/report.md" "$B/incidents/$id/" 2>/dev/null
  head -c 5000 "$d/notes.md" > "$B/incidents/$id/notes.md" 2>/dev/null
  [ -f "$d/evolve/evolve.json" ] && cp "$d/evolve/evolve.json" "$B/incidents/$id/"
done
jq -c 'select(.type=="agent_tool" or (.type|test("incident|evolution|guard_denied|liveness|patrol_|crash_recovery"))) | {t:.t[11:23], ty:.type, i:(.incident//.id), s:.session, tool:.tool, d:.denied, x:((.summary//"")[:110]), r:.rule, o:.outcome}' /var/lib/raiden/events.jsonl > "$B/tool-timeline.jsonl"
for f in $(jq -r '.artifacts_added_or_changed[]?' "$BT/battle.json" 2>/dev/null); do mkdir -p "$B/new/$(dirname "$f")"; cp "/opt/raiden/$f" "$B/new/$f" 2>/dev/null; done
# curated Postgres log lines per category (a crash storm has thousands of lines; a plain head would hide TOAST and DROP)
LOGS=$(ls /var/log/postgresql/raiden-*.json 2>/dev/null)
hl() { jq -c "select($1) | {t:.timestamp[11:23], sev:.error_severity, st:.state_code, m:(.message//\"\")[:170], bt:.backend_type}" $LOGS 2>/dev/null | head -n "$2"; }
{
  hl '(.message//"")|test("terminated by signal")' 3
  hl '(.message//"")|test("terminating any other active")' 2
  hl '(.error_severity//"")=="FATAL" and ((.message//"")|test("administrator command"))' 2
  hl '(.error_severity//"")=="FATAL" and ((.message//"")|test("in recovery mode"))' 2
  hl '(.message//"")|test("all server processes terminated")' 2
  hl '(.message//"")|test("unexpected chunk")' 3
  hl '(.message//"")|test("DROP TABLE|does not exist")' 3
  hl '(.error_severity//"")|test("FATAL|PANIC")' 40
} > "$B/pg-log-highlights.jsonl"
# how many crash-class log lines round 1 produced (what would otherwise have paged somebody)
A=$(jq -r '.rounds[0].attack_at // empty' "$BT/battle.json" 2>/dev/null | sed 's/T/ /; s/Z//' | cut -c1-19)
Z=$(jq -r '.rounds[0].ended_at // empty' "$BT/battle.json" 2>/dev/null | sed 's/T/ /; s/Z//' | cut -c1-19)
N=0
if [ -n "$A" ] && [ -n "$Z" ]; then
  N=$(jq -r --arg a "$A" --arg b "$Z" 'select(.timestamp >= $a and .timestamp <= $b and ((.message//"")|test("was terminated by signal|exited with exit code (?![01]\\b)[0-9]+|terminating any other active server processes|all server processes terminated; reinitializing|database system was not properly shut down|automatic recovery in progress|terminating connection because of crash of another server process"))) | 1' $LOGS 2>/dev/null | wc -l | tr -d ' ')
fi
echo "{\"crash_lines_round1\": ${N:-0}}" > "$B/storm.json"
cp /var/lib/raiden/audit.jsonl "$B/audit.jsonl" 2>/dev/null
tar czf /var/lib/arena/deck-bundle.tgz -C /var/lib/arena deck-bundle
ls -la /var/lib/arena/deck-bundle.tgz; du -sh "$B"; find "$B" -type f | wc -l
