#!/usr/bin/env bash
# Runs ON the VM (via Run Command): collect a compact, curated bundle of the chaos test's results for tests/chaos/results.
set -u
B=/var/lib/arena/results-bundle
rm -rf "$B"; mkdir -p "$B/incidents" "$B/new"
BT=/var/lib/arena/battle
cp "$BT/battle.json" "$BT/timeline_10s.json" "$B/" 2>/dev/null
cp "$BT/events.jsonl" "$B/arena-events.jsonl" 2>/dev/null
for f in lessons.md evolution.md patrol.log protections.list; do cp "/var/lib/raiden/memory/$f" "$B/" 2>/dev/null; done
for d in /var/lib/raiden/incidents/*/; do
  id=$(basename "$d"); mkdir -p "$B/incidents/$id"
  cp "$d/incident.json" "$d/report.md" "$B/incidents/$id/" 2>/dev/null
  head -c 5000 "$d/notes.md" > "$B/incidents/$id/notes.md" 2>/dev/null
  [ -f "$d/evolve/evolve.json" ] && cp "$d/evolve/evolve.json" "$B/incidents/$id/"
done
# every tool call with its FULL command and the time it was issued (audit.jsonl keeps only the first 300 characters, which
# hides the real command of a multi-line call); report.py times the agents' "Key commands" from these
/opt/raiden/venv/bin/python - "$B" <<'PY'
import glob, json, os, re, sys


def command_of(raw):
    """The command of a tool call. The transcript cuts long argument strings, which breaks their JSON: salvage the prefix."""
    try:
        a = json.loads(raw or "{}")
        return str(a.get("command") or a.get("path") or a.get("name") or ""), False
    except ValueError:
        m = re.search(r'"(?:command|path|name)"\s*:\s*"((?:[^"\\]|\\.)*)', raw or "", re.S)
        if not m:
            return "", True
        body = m.group(1)
        while body:
            try:
                return json.loads('"' + body + '"'), True
            except ValueError:
                body = body[:-1]  # drop a dangling half escape sequence
        return "", True


for d in glob.glob("/var/lib/raiden/incidents/*/"):
    out_dir = os.path.join(sys.argv[1], "incidents", os.path.basename(d.rstrip("/")))
    if not os.path.exists(os.path.join(d, "transcript.jsonl")) or not os.path.isdir(out_dir):
        continue
    calls, pending = [], []
    for line in open(os.path.join(d, "transcript.jsonl"), errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("role") == "assistant":
            for c in r.get("calls") or []:
                cmd, cut = command_of(c.get("args"))
                rec = {"t": r["t"], "tool": c.get("name"), "cmd": cmd[:3000], **({"truncated": True} if cut else {})}
                calls.append(rec)
                pending.append(rec)
        elif r.get("role") == "tool":  # results come back in call order
            for rec in pending:
                if rec["tool"] == r.get("name") and "denied" not in rec:
                    rec["denied"] = bool(r.get("denied"))
                    break
    with open(os.path.join(out_dir, "calls.jsonl"), "w") as fh:
        fh.writelines(json.dumps(c) + "\n" for c in calls)
PY
jq -c 'select(.type=="agent_tool" or (.type|test("incident|evolution|guard_denied|liveness|patrol_|crash_recovery"))) | {t:.t[11:23], ty:.type, i:(.incident//.id), s:.session, tool:.tool, d:.denied, x:((.summary//"")[:110]), r:.rule, o:.outcome}' /var/lib/raiden/events.jsonl > "$B/tool-timeline.jsonl"
for f in $(jq -r '.artifacts_added_or_changed[]?' "$BT/battle.json" 2>/dev/null); do mkdir -p "$B/new/$(dirname "$f")"; cp "/opt/raiden/$f" "$B/new/$f" 2>/dev/null; done
# curated Postgres log lines per category (a crash storm has thousands of lines; a plain head would hide TOAST and DROP)
LOGS=$(ls /var/log/postgresql/raiden-*.json 2>/dev/null)
hl() { jq -c "select($1) | {t:.timestamp[11:23], sev:.error_severity, st:.state_code, m:(.message//\"\")[:170], bt:.backend_type}" $LOGS 2>/dev/null | head -n "$2"; }
{
  hl '(.message//"")|test("terminated by signal")' 3
  hl '(.message//"")|test("terminating any other active")' 2
  hl '(.message//"")|test("raiden protection|ALTER SYSTEM is not allowed")' 8
  hl '(.error_severity//"")=="FATAL" and ((.message//"")|test("administrator command"))' 2
  hl '(.error_severity//"")=="FATAL" and ((.message//"")|test("in recovery mode"))' 2
  hl '(.message//"")|test("all server processes terminated")' 2
  hl '(.message//"")|test("unexpected chunk")' 3
  hl '(.message//"")|test("DROP TABLE|does not exist")' 3
  hl '(.message//"")|test("connection slots are reserved|too many clients")' 3
  hl '(.message//"")|test("too many connections for role")' 2
  hl '(.message//"")|test("still waiting for [A-Za-z]+Lock on relation")' 3
  hl '(.error_severity//"")|test("FATAL|PANIC")' 40
} > "$B/pg-log-highlights.jsonl"
# how many crash-class log lines round 1 produced (what would otherwise have paged somebody)
A=$(jq -r '.rounds[] | select(.round=="crash") | .attack_at // empty' "$BT/battle.json" 2>/dev/null | sed 's/T/ /; s/Z//' | cut -c1-19)
Z=$(jq -r '.rounds[] | select(.round=="crash") | .ended_at // empty' "$BT/battle.json" 2>/dev/null | sed 's/T/ /; s/Z//' | cut -c1-19)
N=0
if [ -n "$A" ] && [ -n "$Z" ]; then
  N=$(jq -r --arg a "$A" --arg b "$Z" 'select(.timestamp >= $a and .timestamp <= $b and ((.message//"")|test("was terminated by signal|exited with exit code (?![01]\\b)[0-9]+|terminating any other active server processes|all server processes terminated; reinitializing|database system was not properly shut down|automatic recovery in progress|terminating connection because of crash of another server process"))) | 1' $LOGS 2>/dev/null | wc -l | tr -d ' ')
fi
echo "{\"crash_lines_round1\": ${N:-0}}" > "$B/storm.json"
cp /var/lib/raiden/audit.jsonl "$B/audit.jsonl" 2>/dev/null
tar czf /var/lib/arena/results-bundle.tgz -C /var/lib/arena results-bundle
ls -la /var/lib/arena/results-bundle.tgz; du -sh "$B"; find "$B" -type f | wc -l
