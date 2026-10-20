---
name: hardening-after-incident
description: Close-out and level-up after an incident is stable. PART A (inside the incident session) - write $RAIDEN_INCIDENT_DIR/report.md with the fixed template, append 1-3 lessons to /var/lib/raiden/memory/lessons.md, final RESOLUTION/SUMMARY lines. PART B (separate "evolve" session after the incident closes) - add durable protections (new patrol check, learned skill, skill pitfall update, safe config hardening), validate each, log them in /var/lib/raiden/memory/evolution.md.
---

# hardening-after-incident

## PART A - during the incident session (short; do not start building defenses here)

### A1. Report: `$RAIDEN_INCIDENT_DIR/report.md` (exact headings)
```markdown
# Incident <id> — <title>
## Timeline (UTC)
- 12:01:02 first `<error signature>` in the log ... (one line per event: detection, actions, recovery)
## Impact
What users/data were affected, how long; downtime; data at risk; reconcile result.
## Root cause
The evidence chain (log lines, settings with sourcefile:line, process names), not guesses.
## What I did
Only the commands that mattered, in order, each with its result.
## Verification
Probes and their output; reconcile JSON key numbers (acked, lost, corrupt, unreadable, ok).
## Follow-ups
Prevention ideas for PART B, leftovers for humans (e.g. staging tables, escalations).
```
Sources: `$RAIDEN_INCIDENT_DIR/notes.md`, the jsonlog, snapshot files, reconcile outputs.

### A2. Lessons: append 1-3 factual lines to /var/lib/raiden/memory/lessons.md
Format: `- [YYYY-MM-DD] <symptom signature> => <cause> => <fix that worked> (skill: <name>)`
```bash
grep -iF '<key words of the signature>' /var/lib/raiden/memory/lessons.md     # avoid duplicates; refine instead
echo "- [$(date -u +%F)] repeated 'terminated by signal 9' in random backends + checkpointer, no OOM in kernel log => unexpected library in shared_preload_libraries (conf.d/<file>:<line>) => commented it out, 1 restart, reconcile ok (skill: crash-loop-rca)" >> /var/lib/raiden/memory/lessons.md
```

### A3. Final answer: last two lines exactly
`RESOLUTION: resolved|mitigated|escalated` and `SUMMARY: <one line>`.
resolved = cause removed and verified (reconcile ok if data could be affected); mitigated = bleeding
stopped, cause or verification open; escalated = /var/lib/raiden/escalations/$RAIDEN_INCIDENT_ID.md written.

## PART B - evolve session (after the incident closed)
Inputs: the incident's report.md and notes.md, lessons.md, evolution.md (what exists already),
`ls /opt/raiden/checks.d /opt/raiden/.claude/skills`. Ask: what would have PREVENTED this, DETECTED it
earlier, or made the fix FASTER next time? Implement the 1-3 cheapest high-value artifacts:

### B1. Patrol check `/opt/raiden/checks.d/NN-<name>.sh` (typical: preload-library allowlist + unknown
background-worker detector, config drift vs a stored checksum baseline, crash counter, amcheck sample, restore drill)
Compare what is PLANNED with what RUNS (`postgres -C <guc>` or `pg_settings.pending_restart` vs the baseline): a
restart-only setting that sits on disk but is not live yet is a time bomb for the next restart, and a check that only
looks at the running server finds it too late. Patrol also runs right after every config reload seen in the log.
Contract: last line `OK|WARN|CRIT <name>: <message>`, exit 0/1/2, < 15 s, never hangs, DB down => WARN/CRIT.
```bash
#!/usr/bin/env bash
# NN-name.sh - <what it detects>. On WARN/CRIT use skill: <skill>. Added after incident <id>.
set -euo pipefail
NAME=name; export PGCONNECT_TIMEOUT=3
q() { timeout 10 psql -X -At -v ON_ERROR_STOP=1 -d postgres -c "$1"; }
if ! v="$(q "SELECT ...")"; then echo "WARN $NAME: database unreachable, check skipped"; exit 1; fi
if [ "$v" != "expected" ]; then echo "CRIT $NAME: found '$v', expected 'expected'"; exit 2; fi
echo "OK $NAME: $v"; exit 0
```
Baselines it needs (allowlists, checksums) go in /var/lib/raiden/memory/<name>.baseline, never in /etc/raiden.

### B2. Learned skill `/opt/raiden/.claude/skills/learned-<topic>/SKILL.md` (30-60 lines) or a Pitfalls
update (append a bullet; do not rewrite) in an existing skill.
```markdown
---
name: learned-<topic>
description: <exact error signature / symptom as it appears in the log> - <cause> - <what this skill does>. Use when <trigger>.
---
# learned-<topic>
## Signature   (log lines, SQLSTATE, process names that identify it)
## Confirm     (2-3 read-only commands with expected output)
## Fix         (exact commands, in order; restarts counted)
## Verify      (probes + reconcile if data-affecting)
## Origin      incident <id>, <date>
```
`name` must equal the directory name (lowercase, hyphens); the description is what makes the agent pick it.

### B3. Config hardening (reloadable, safe GUCs only)
`sudo -n -u postgres tee /etc/postgresql/18/main/conf.d/20-raiden-<topic>.conf` with e.g. `log_lock_waits = on`,
`log_autovacuum_min_duration = '10s'`, `log_temp_files = '100MB'`, `checkpoint_completion_target = 0.9`;
per-role limits via `ALTER ROLE arena SET idle_in_transaction_session_timeout = '60s'`. Then
`SELECT pg_reload_conf();`, check `SELECT * FROM pg_file_settings WHERE error IS NOT NULL` is empty and `SHOW <guc>`.
Restart-only settings: list as a follow-up, do not restart for hardening.

### B4. Validate every artifact (show the evidence in evolution.md)
- scripts: `bash -n <file>`, `shellcheck <file>` if installed, `chmod 755`, run once: output + exit code + runtime
  (`time`); simulate DB down: `PGHOST=/tmp/raiden-nosuchdir <check>` must answer WARN/CRIT within 15 s;
- `/opt/raiden/.claude/skills/health-patrol/scripts/run_checks.sh` still runs all checks;
- skills: frontmatter `name` == directory, description contains the signature; config: no pg_file_settings errors.

### B5. Record: append one paragraph to /var/lib/raiden/memory/evolution.md
`## <UTC date> after incident <id>` + what was added (full paths), what each protects against, how it was
validated (exit codes/outputs), and what was considered but not done (and why).

### Forbidden in PART B
Editing /opt/raiden/raiden/** or /etc/raiden/**, deleting anything, weakening durability, restarting
PostgreSQL, raising thresholds or disabling checks to silence a real alert, network installs.
