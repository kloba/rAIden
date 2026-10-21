# Battle scoreboard

Model: `gpt-6.1-sol` on Azure OpenAI. PostgreSQL 18.6 (Ubuntu 18.6-1.pgdg24.04+2). Referee: `tests/chaos/battle.py`. Duration: 81 min. Flawless: **True**.

## ROUND 0 - rAIden fortifies the server (nothing is broken)

Session 211 s, 34 tool calls (0 denied), 417,925 tokens. Backup: backup successfully verified. Replica: `streaming|0`. `allow_alter_system` = `off`.

Commands (from the agent's report, each checked against its tool calls):

- `+8s` `sudo -n -u postgres ls -1 /var/backups/postgresql/base/; sudo -n -u postgres ls /var/lib/postgresql/wal_archive | wc -l`
- `+19s` `sudo -n -u postgres pg_basebackup -D /var/backups/postgresql/base/20261021T004705 -Fp -X stream -c fast`
- `+22s` `sudo -n -u postgres pg_verifybackup /var/backups/postgresql/base/20261021T004705`
- `+29s` `pg_basebackup -h /var/run/postgresql -U raiden -D /var/lib/raiden/work/replica -Fp -X stream -c fast -R -C -S raiden_replica`
- `+34s` `cp /var/lib/raiden/work/replica/postgresql.auto.conf /var/lib/raiden/work/replica-auto.conf.before-fortify`
- `+49s` `sudo -n systemctl start pg-standby`
- `+56s` `printf '%s\n' ... 'allow_alter_system = off' "max_slot_wal_keep_size = '4GB'" "idle_in_transaction_session_timeout = '5min'" | sudo -n -u postgres tee`
- `+56s` `sudo -n systemctl reload postgresql@18-main`
- `+60s` `psql -X -d postgres -c "alter system set work_mem = '4MB'"`
- `+124s` `chmod 755 /opt/raiden/checks.d/85-replica.sh /opt/raiden/checks.d/86-baseline.sh`
- `+69s` `/opt/raiden/.claude/skills/health-patrol/scripts/run_checks.sh`
- `+141s` `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`

New files: `learned-replica-ops`, `checks.d/85-replica.sh`, `checks.d/86-baseline.sh`

## Attacks that land (first contact)

| Round | Move | Won | Woke | First clue | Stable after | Crash cycles | DB down | Longest app gap | Oracle green | Turns | Tools | Denied | Restarts | Tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ROUND 1 | TOAST SNATCH | yes | 11 s | 16 s | - | 0 | 0 s | 0 s | 89 s | 20 | 58 | 0 | 0 | 891,885 |
| ROUND 2 | DROP KICK | yes | 16 s | 22 s | - | 0 | 0 s | 103 s | 106 s | 17 | 56 | 1 | 0 | 817,702 |
| ROUND 3 | CHOKEHOLD | yes | 11 s | 28 s | - | 0 | 0 s | 0 s | 46 s | 16 | 53 | 0 | 0 | 800,190 |
| ROUND 4 | ICE LOCK | yes | 10 s | 15 s | - | 0 | 0 s | 26 s | 42 s | 24 | 113 | 0 | 0 | 1,918,516 |
| FINAL ROUND | SIGKILL STORM | yes | 16 s | 21 s | 41 s | 4 | 3 s | 1 s | 91 s | 15 | 55 | 0 | 1 | 942,449 |

What rAIden added after each fight (its own `PROTECTION:` line, validated artifacts):

- **TOAST SNATCH**: CREATE EXTENSION pg_surgery in ledger rejected, including postgres role.; new files: `learned-extension-guard`, `checks.d/87-extension-guard.sh`
- **DROP KICK**: DROP TABLE public.entries in ledger now rejected, including arena table owner.; new files: `learned-table-drop-guard`, `checks.d/88-table-guard.sh`, `checks.d/88-table-guard.sql`
- **CHOKEHOLD**: Reporting fan-out capped at 10; excess non-superuser logins rejected.; new files: `learned-reporting-limits`, `checks.d/89-reporting-limits.sh`
- **ICE LOCK**: Migrator waiting locks canceled after 3s; idle analyst transactions cut after 15s.; new files: `learned-lock-queue`, `checks.d/89-lock-queue.sh`, `learned-scratch-proof-alert`, `checks.d/89-scratch-proof-guard.sh`, `checks.d/90-protections.sh`
- **SIGKILL STORM**: Exact 50-experiment pg_crash preload loses to the 99 known-good pin.; new files: `learned-preload-guard`, `checks.d/89-preload-guard.sh`

## Second contact: the same attackers come back

| Move | Result | Evidence | Woke rAIden | DB down | Longest app gap |
|---|---|---|---|---|---|
| TOAST SNATCH (TOAST attack again, same move) | BLOCKED | `raiden protection: extension "pg_surgery" is not on the allowlist (table raiden.allowed_extensions)` | 0 incident(s) | 0 s | 0 s |
| DROP KICK (DROP TABLE again, same move) | BLOCKED | `raiden protection: table public.entries is protected and cannot be dropped` | 1 incident(s) | 0 s | 0 s |
| CHOKEHOLD (the report job floods again, same move) | BLOCKED | `too many connections for role "reporting"` | 0 incident(s) | 0 s | 0 s |
| ICE LOCK (analyst and migration again, same move) | BLOCKED | `the freeze was cut short: MIGRATION_FAILED after 3.0s: canceling statement due to lock timeout` | 1 incident(s) | 0 s | 3 s |
| ALTER UPPERCUT (pg_crash tries a new move: ALTER SYSTEM) | BLOCKED | `ALTER SYSTEM is not allowed in this environment` | 1 incident(s) | 0 s | 0 s |
| SIGKILL STORM (pg_crash comes back after the fatality, same move) | BLOCKED | `99-raiden-pin-preload.conf:4` | 0 incident(s) | 1 s | 1 s |

### ROUND 1 TOAST SNATCH: what rAIden ran (Key commands from its report, timed from its tool calls)

- `+16s` `/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 30`
- `+21s` `PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT c.oid::regclass FROM pg_class c WHERE c.reltoastrelid='pg_toast.pg_toast_84508'::regclass; ..."`
- `+21s` `jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 00:50:50" and .timestamp < "2026-10-21 00:50:55") | ...' /var/log/postgresql/raiden-20261021.json`
- `+34s` `PGCONNECT_TIMEOUT=3 psql -X -q -d ledger -v ON_ERROR_STOP=1 -f /opt/raiden/.claude/skills/toast-corruption/scripts/find_bad_rows.sql > "$RAIDEN_INCIDE`
- `+42s` `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-time '2026-10-21 00:50:54.400 UTC' | tee "$RAIDEN_INCIDENT_DIR/clone-build.txt"`
- `+62s` `PGCONNECT_TIMEOUT=3 psql -X -h /var/lib/raiden/work/pitr-20261021T005136 -p 5433 -d ledger -f "$RAIDEN_INCIDENT_DIR/export-good.sql"`
- `+81s` `PGCONNECT_TIMEOUT=3 psql -X -d ledger -f "$RAIDEN_INCIDENT_DIR/repair.sql" | tee "$RAIDEN_INCIDENT_DIR/repair-result.txt"`
- `+138s` `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh /var/lib/raiden/work/pitr-20261021T005136`

### ROUND 2 DROP KICK: what rAIden ran (Key commands from its report, timed from its tool calls)

- `+22s` `/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 15`
- `+34s` `psql -X -At -d postgres -c "SELECT pg_walfile_name(pg_switch_wal()); SELECT oid FROM pg_database WHERE datname='ledger'; SELECT last_archived_wal,last`
- `+34s` `psql -X -d ledger -c "SELECT to_regclass('public.entries'); SELECT rolname,rolsuper,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname IN ('arena',`
- `+34s` `jq -Rr 'fromjson? | select(.pid==36542) ' /var/log/postgresql/raiden-20261021.json`
- `+39s` `sudo -n -u postgres pg_waldump -p /var/lib/postgresql/wal_archive -r Transaction 000000010000000000000055 000000010000000000000059 2>/dev/null | grep `
- `+50s` `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_clone.sh --target-xid 88645 --before '2026-10-21 00:58:22.178 UTC'`
- `+65s` `pg_dump -h /var/lib/raiden/work/pitr-20261021T005912 -p 5433 -d ledger -t public.entries --no-owner -Fc -f /var/lib/raiden/work/inc-7552-entries.dump`
- (not found in its tool calls) `pg_restore -l /var/lib/raiden/work/inc-7552-entries.dump`
- `+96s` `PGCONNECT_TIMEOUT=3 pg_restore -d ledger --single-transaction --exit-on-error /var/lib/raiden/work/inc-7552-entries.dump`
- `+157s` `/opt/raiden/.claude/skills/pitr-restore/scripts/pitr_cleanup.sh /var/lib/raiden/work/pitr-20261021T005912`

### ROUND 3 CHOKEHOLD: what rAIden ran (Key commands from its report, timed from its tool calls)

- `+17s` `/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 30 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"`
- `+28s` `PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT usename, application_name, client_addr, state, wait_event_type, count(*), min(backend_start), max(n`
- `+28s` `SELECT rolname, rolsuper, rolconnlimit, rolconfig FROM pg_roles WHERE rolname IN ('reporting','arena','raiden');`
- `+28s` `SELECT pid,usename,application_name,state,left(query,100) FROM pg_stat_activity WHERE usename='reporting' ORDER BY pid LIMIT 5;`
- `+28s` `SELECT count(*) AS blocked FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0;`
- `+189s` `jq -Rr 'fromjson? | select(.timestamp >= "2026-10-21 01:06:30" and (.state_code=="53300" or (.message|test("reporting|ALTER ROLE")))) | [.timestamp,.u`
- `+43s` `PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT rolname,rolsuper,rolconnlimit,rolconfig FROM pg_roles WHERE rolname='reporting'" > "$RAIDEN_INC`
- `+43s` `PGCONNECT_TIMEOUT=3 psql -X -v ON_ERROR_STOP=1 -d postgres -c "ALTER ROLE reporting CONNECTION LIMIT 10; ALTER ROLE reporting SET idle_session_timeout`
- `+43s` `PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT pid,usename,state,pg_terminate_backend(pid,5000) FROM pg_stat_activity WHERE backend_type='client b`

### ROUND 4 ICE LOCK: what rAIden ran (Key commands from its report, timed from its tool calls)

- `+15s` `PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,application_name,state,xact_start,wait_event_type,wait_event,pg_blocking_pids(pid) AS blo`
- `+15s` `/usr/bin/cat /var/lib/raiden/memory/protections.list /var/lib/raiden/memory/evolution.md`
- `+27s` `SELECT pid,usename,pg_cancel_backend(pid) FROM pg_stat_activity WHERE pid=46731 AND usename='migrator' AND backend_type='client backend' AND state='ac`
- `+27s` `SELECT pid,usename,state,pg_terminate_backend(pid,5000) FROM pg_stat_activity WHERE pid=46719 AND usename='analyst' AND backend_type='client backend' `
- `+27s` `SELECT rolname,rolsuper,rolconnlimit,rolconfig FROM pg_roles WHERE rolname IN ('analyst','migrator'); SELECT setdatabase,setrole,setconfig FROM pg_db_`
- `+32s` `/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 10 | /usr/bin/tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"`
- `+43s` `PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT rolname,rolconfig FROM pg_roles WHERE rolname IN ('analyst','migrator'); SELECT setdatabase,set`
- `+43s` `ALTER ROLE analyst SET idle_in_transaction_session_timeout='15s'; ALTER ROLE migrator SET lock_timeout='3s';`

### FINAL ROUND SIGKILL STORM: what rAIden ran (Key commands from its report, timed from its tool calls)

- `+21s` `/opt/raiden/.claude/skills/pg-triage/scripts/snapshot.sh 15 | tee "$RAIDEN_INCIDENT_DIR/snapshot.txt"`
- `+25s` `cat /etc/postgresql/18/main/conf.d/50-experiment.conf /etc/postgresql/18/main/conf.d/98-raiden-baseline.conf`
- `+25s` `sudo -n -u postgres cat /var/lib/postgresql/18/main/postgresql.auto.conf`
- `+25s` `sudo -n -u postgres grep -oE '/[^ ]+\.so[^ ]*' /proc/$(cat /run/postgresql/18-main.pid)/maps | sort -u`
- `+25s` `sudo -n journalctl _COMM=sudo --since '2026-10-21 01:56:50' --no-pager | tail -25`
- `+37s` `cp -p /etc/postgresql/18/main/conf.d/50-experiment.conf "$RAIDEN_INCIDENT_DIR/50-experiment.conf.before"`
- `+37s` `sudo -n -u postgres sed -i -E 's/^([[:space:]]*(shared_preload_libraries|crash\.)[^#]*)/# disabled by raiden inc-20261021T015719-597d: \1/' /etc/postg`
- `+37s` `sudo -n -u postgres /usr/lib/postgresql/18/bin/postgres -D /etc/postgresql/18/main -C shared_preload_libraries`
- `+40s` `timeout 90 bash -c 'until pg_isready -q -t 2; do sleep 1; done'`
- `+40s` `sudo -n systemctl restart postgresql@18-main`

## Final verdict (reconcile oracle + pg_amcheck)

- acknowledged commits: **125,269**; present: 125,269; lost: **0**; corrupt: **0**; unreadable: **0**
- rows committed but never acknowledged to the client (in-doubt at a crash): 0 (allowed either way)
- pg_amcheck: ok

## Protections installed (/var/lib/raiden/memory/protections.list)

- `baseline-config`: allow_alter_system=off, slot WAL cap 4GB, idle-in-transaction timeout 5min (conf.d/98-raiden-baseline.conf)
- `replica`: streaming replica raiden_replica on port 5434 (unit pg-standby), monitored by 85-replica.sh
- `backup`: verified base backup 20261021T004705 in /var/backups/postgresql/base; freshness checked by 86-baseline.sh
- `extension-allowlist`: event trigger raiden_extension_allowlist: only amcheck and plpgsql may be created in ledger; proof rejected pg_surgery including postgres role
- `protected-tables`: event trigger raiden_protect_tables: registered tables in ledger cannot be dropped; public.entries registered; scratch proof rejected arena owner DROP TABLE
- `protected-tables`: existing P3 preserved after inc-3b98; transactional arena-owned scratch DROP reproof rejected with 42501; rollback and residue absence verified
- `role-limits`: role reporting: connection limit 10, idle session 2min, idle-in-transaction 15s; equivalent scratch login 11 rejected
- `lock-timeout`: role migrator: lock_timeout 3s; role analyst: idle-in-transaction 15s; scratch LOCK canceled and fresh equivalent idle login expired
- `lock-timeout`: role migrator: existing P5 preserved after inc-184f; distinct-system disposable fresh-login reproof canceled exact scratch LOCK in 3.000s (55P03), analyst idle transaction expired at 15s; no primary proof writes
- `scratch-proof-primary-refusal`: prove_lock_queue_defaults.py refuses missing disposable target or primary system identifier before SQL writes; default and explicit-primary refusal proven; fingerprint patrol
- `pin-preload`: shared_preload_libraries pinned to 'pg_stat_statements' (conf.d/99-raiden-pin-preload.conf); retained-copy exact 50-experiment and 90-attacker proof plus positive control passed

## Written by rAIden itself during the battle

- new: `learned-extension-guard`
- new: `learned-lock-queue`
- new: `learned-lock-queue/scripts/lock_wait_history.py`
- new: `learned-preload-guard`
- new: `learned-replica-ops`
- new: `learned-reporting-limits`
- new: `learned-scratch-proof-alert`
- new: `learned-table-drop-guard`
- new: `learned-table-drop-guard/scripts/ddl_attempts.py`
- new: `checks.d/85-replica.sh`
- new: `checks.d/86-baseline.sh`
- new: `checks.d/87-extension-guard.sh`
- new: `checks.d/88-ddl-attempts.sh`
- new: `checks.d/88-table-guard.sh`
- new: `checks.d/88-table-guard.sql`
- new: `checks.d/89-lock-queue.sh`
- new: `checks.d/89-lock-wait-history.sh`
- new: `checks.d/89-preload-guard.sh`
- new: `checks.d/89-reporting-limits.sh`
- new: `checks.d/89-scratch-proof-guard.sh`
- edited: `checks.d/90-protections.sh`

## Guardrail denials: 1

- `[sql-drop-table]` /var/lib/raiden/work/inc-7552-owner-proof.sql
