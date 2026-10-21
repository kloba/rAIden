# Incident inc-20261021T015242-50c1 — Preload change rejected by existing protection
## Timeline (UTC)
- 01:52:35.154: local psql, postgres role/database, PID 63242 attempted to add pg_crash through ALTER SYSTEM; same session immediately received ERROR 0A000.
- 01:52:47: initial triage confirmed healthy primary and unchanged preload; no crashes or recovery.
- Before 01:53:18: correlated statement/rejection, checked live and on-disk configuration, empty auto.conf, intact baseline/protection checks. Culprit session already gone.
- 01:53:18: oracle passed and planned startup preload validated; replica streaming with zero-byte lag.
- 01:54:23: observation through 108 seconds after the attempt found only the original statement/rejection, no subsequent preload changes, reloads or crashes.
- 01:54:26: final probe healthy, same postmaster, six restarts left.
## Impact
One administrative statement failed. No observed application downtime or crash recovery; no database data/configuration change from the attempted statement. Oracle verified all 103,549 acknowledged commits present, zero lost/corrupt/unreadable. Its 99 unrecorded rows occurred during concurrent traffic and are not acknowledged-commit loss.
## Root cause
A local postgres-role psql session attempted `ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements', 'pg_crash';`. JSON log PID 63242/session 6ad81ae3.f70a records both statement and ERROR 0A000 at 01:52:35.154. Existing `allow_alter_system=off`, sourced from `/etc/postgresql/18/main/conf.d/98-raiden-baseline.conf:2`, rejected it before any auto.conf change. Live and applied file preload remains `pg_stat_statements` from `10-raiden-baseline.conf:5`; auto.conf contains only header comments. No evidence of loaded pg_crash, unusual workers, or crashes. This is a successful preventive refusal, not a crash-loop incident. Logs attribute the database role/session, not the human/process intent behind it.
## Key commands
```text
/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 10 | tee "$RAIDEN_INCIDENT_DIR/snapshot-initial.txt"
jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:52:00" and (.pid == 63242 or (.message|test("ALTER SYSTEM|pg_crash|SIGHUP|configuration file|terminated by signal|reinitializing"))))' /var/log/postgresql/raiden-20261021.json
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT name,setting,source,sourcefile,sourceline,pending_restart FROM pg_settings WHERE name IN ('allow_alter_system','shared_preload_libraries','fsync','full_page_writes','synchronous_commit'); ..."
SELECT sourcefile,sourceline,name,setting,applied,error FROM pg_file_settings WHERE name IN ('shared_preload_libraries','allow_alter_system') OR error IS NOT NULL;
SELECT pid,usename,application_name,state,query FROM pg_stat_activity WHERE pid=63242;
sudo -n -u postgres cat /var/lib/postgresql/18/main/postgresql.auto.conf
/opt/raiden/checks.d/86-baseline.sh
/opt/raiden/checks.d/90-protections.sh
```
The two standalone SELECT lines are portions of the same executed psql command above. No remediation SQL, configuration edit, reload, restart or termination was performed: existing protection was the effective fix.
## What I did
Read the entry-point and matching runbooks, captured triage evidence, correlated exact session/PID statement and refusal, and checked both running and future-start configuration. Preserved existing protection without intervention; backend had disconnected and no bad setting existed to remove. Ran read-only integrity oracle, replica verification and a 65-second observation (ending 108 seconds after the attempt). Wrote incident evidence and backed up lessons before appending one factual lesson. Built no new defenses during the incident.
## Verification
- Initial/final pg_probe: ready=true, in_recovery=false, normal worker types, seven clients, oldest transaction=0s, checksum_failures=0, crash_lines_last_300kb=0, active service. Unchanged postmaster start 00:45:54.446158Z; restarts_left=6.
- `pg_settings`: allow_alter_system off, shared_preload_libraries pg_stat_statements; fsync/full_page_writes/synchronous_commit on. No pending restart; pg_file_settings error count=0.
- `sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /var/lib/postgresql/18/main -c config_file=/etc/postgresql/18/main/postgresql.conf -C shared_preload_libraries`: pg_stat_statements.
- Baseline check OK; protections check OK (8 applicable records verified, including repeated attestations rather than 8 independent guards).
- `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`: ok=true, acked=present=103549, lost=corrupt=unreadable=0, unrecorded_rows_in_db=99; evidence in reconcile.json.
- Replica status helper: healthy, active slot, streaming asynchronous replica, zero-byte lag.
- preload-observation.txt: original attempted statement and rejection only; no new crash/recovery/config-change events in the selected observation interval.
## Follow-ups
- Separate evolve session may add precise rejected-ALTER-SYSTEM attribution/history and check live versus planned preload against an independent allowlist. Do not weaken ALTER SYSTEM protection or re-run a dangerous preload attempt as a proof.
- Review privileged local postgres access with its owner; role/session attribution does not establish the operator's intent. No evidence warrants disruptive privilege changes here.
- Existing ALTER SYSTEM block does not prevent privileged OS edits to configuration. Same-host replica/backups remain vulnerable to host loss; off-host retention and access review remain separate work.

RESOLUTION: resolved
SUMMARY: Existing ALTER SYSTEM guard rejected pg_crash preload change; configuration intact, no restart needed, all 103,549 acknowledged commits verified.
