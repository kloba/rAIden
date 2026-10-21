---
name: fortify-database
description: "Day-0 hardening when there is NO incident (task says FORTIFY SESSION): take a verified base backup (pg_basebackup + pg_verifybackup), build a streaming replica on port 5434 (slot raiden_replica, unit pg-standby), apply reloadable baseline config (allow_alter_system off, WAL cap for the slot), write two patrol checks and one learned skill, record protections.list, report with a Key commands section."
---

# fortify-database - make the server harder to hurt BEFORE something happens

Nothing is broken. Work calmly, one step per tool call, and verify each step. Shell variables do NOT persist between
tool calls: take the timestamp once (`date -u +%Y%m%dT%H%M%S`) and then type it literally. No PostgreSQL restart in this
session (everything below is reloadable). Never touch application data. Notes: `$RAIDEN_INCIDENT_DIR/notes.md`.

## 0. Look first (read-only)
```bash
psql -X -At -d postgres -c "select version()"
sudo -n -u postgres ls -1 /var/backups/postgresql/base/; sudo -n -u postgres ls /var/lib/postgresql/wal_archive | wc -l
psql -X -d postgres -c "select slot_name, active from pg_replication_slots"
psql -X -At -d postgres -c "select name||'='||setting from pg_settings where name in ('wal_level','archive_mode','max_wal_senders','max_connections','allow_alter_system','shared_preload_libraries')"
ls -l /etc/postgresql/18/main/conf.d/; sudo -n systemctl status pg-standby --no-pager | head -3
```
Typical finding: WAL archiving is on but there is NO base backup, so point-in-time recovery is impossible; no replica;
ALTER SYSTEM is open. Say what you found in notes.md before you change anything.

## 1. BACKUP: base backup + proof that it is restorable
```bash
sudo -n -u postgres pg_basebackup -D /var/backups/postgresql/base/<TS> -Fp -X stream -c fast
sudo -n -u postgres pg_verifybackup /var/backups/postgresql/base/<TS>       # must end with: backup successfully verified
```
`<TS>` is UTC like `20261020T101500`. Do not delete or move anything below /var/backups/postgresql.

## 2. REPLICA: streaming standby on this host (slot raiden_replica, port 5434, socket dir /tmp)
A replica is a second live copy, NOT a backup: it replays mistakes (DROP TABLE, corruption) just as faithfully.
a) Clone the primary into the work area as the raiden OS user (the unix socket is peer-authenticated) and create the slot:
```bash
pg_basebackup -h /var/run/postgresql -U raiden -D /var/lib/raiden/work/replica -Fp -X stream -c fast -R -C -S raiden_replica
```
`-R` writes standby.signal, primary_conninfo and primary_slot_name. If the directory or slot already exists (re-run):
`sudo -n systemctl stop pg-standby`, `rm -rf /var/lib/raiden/work/replica`, and
`psql -X -d postgres -c "select pg_drop_replication_slot('raiden_replica')"` (only if it is inactive).
b) The Debian layout keeps config outside PGDATA, so give the replica its own three files (write_file for the first two):
`/var/lib/raiden/work/replica/postgresql.conf` = a one-line comment; `/var/lib/raiden/work/replica/pg_hba.conf` = `local all all peer`;
`/var/lib/raiden/work/replica/pg_ident.conf` = a one-line comment. Then append the overrides (the LAST entry wins, never use ALTER SYSTEM here):
```bash
cat >> /var/lib/raiden/work/replica/postgresql.auto.conf <<'CONF'
# --- rAIden replica overrides ---
port = 5434
listen_addresses = ''
unix_socket_directories = '/tmp'
hot_standby = on
shared_preload_libraries = ''
archive_mode = off
logging_collector = off
log_destination = 'stderr'
shared_buffers = '256MB'
cluster_name = 'raiden-replica'
CONF
```
A hot standby needs max_connections, max_worker_processes, max_wal_senders, max_prepared_transactions and
max_locks_per_transaction >= the primary's: compare with `psql -X -At -d postgres -c "select name||'='||setting from pg_settings where name in (...)"`
and add any that differ to the overrides.
c) Start it as a service (a normal user cannot keep a daemon alive) and wait until it accepts connections:
```bash
sudo -n systemctl start pg-standby
timeout 90 bash -c 'until pg_isready -q -h /tmp -p 5434 -t 2; do sleep 1; done'; echo rc=$?
```
Failure: `sudo -n journalctl -u pg-standby -n 40 --no-pager` (typical: a missing pg_hba.conf, or "hot standby is not possible
because ... is a lower setting than on the primary").
d) Prove it streams (both lines must look right: state=streaming, lag_bytes small, in_recovery=t, rows readable):
```bash
psql -X -d postgres -c "select slot_name, active from pg_replication_slots where slot_name='raiden_replica'"
psql -X -d postgres -c "select state, sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) as lag_bytes from pg_stat_replication"
psql -X -At -h /tmp -p 5434 -d ledger -c "select pg_is_in_recovery(), count(*) from public.entries"
/opt/raiden/.claude/skills/fortify-database/scripts/replica_status.sh      # one-screen summary, exit 0 = healthy
```

## 3. BASELINE: reloadable hardening in conf.d/98-raiden-baseline.conf
conf.d is processed alphabetically, later wins; 98 sorts after the lab's 10-/20-/50- files.
```bash
printf '%s\n' '# rAIden baseline hardening (fortify session). Reloadable settings only. Undo: remove this file and reload.' \
  'allow_alter_system = off' "max_slot_wal_keep_size = '4GB'" "idle_in_transaction_session_timeout = '5min'" \
  | sudo -n -u postgres tee /etc/postgresql/18/main/conf.d/98-raiden-baseline.conf
sudo -n systemctl reload postgresql@18-main
psql -X -At -d postgres -c "select name||'='||setting||' ('||coalesce(sourcefile,'-')||')' from pg_settings where name in ('allow_alter_system','max_slot_wal_keep_size','idle_in_transaction_session_timeout')"
psql -X -At -d postgres -c "select count(*) from pg_file_settings where error is not null"      # must be 0
```
Why: `allow_alter_system = off` makes ALTER SYSTEM fail, so postgresql.auto.conf (which is read LAST and overrides every file
in conf.d) cannot be used to sneak settings in; `max_slot_wal_keep_size` stops a dead replica from filling the disk through
its slot; idle-in-transaction sessions cannot hold locks forever. Prove the first one without changing anything real:
`psql -X -d postgres -c "alter system set work_mem = '4MB'"` must fail with `ALTER SYSTEM is not allowed in this environment`.
Never set fsync/full_page_writes/synchronous_commit off. Do not touch shared_preload_libraries in this session.

## 4. NEW ARTIFACTS (you write them; contract: skill hardening-after-incident B1/B2)
Write each with write_file, `chmod 755` the checks, RUN each once and show the output.
1. `/opt/raiden/checks.d/85-replica.sh`: is the slot active and a walsender streaming, lag below 64 MB? Tolerate a primary that
   restarted in the last 90 s (`pg_postmaster_start_time()`): the standby needs a few seconds to reconnect, answer OK "reconnecting".
   CRIT only when the replica has been gone for long (slot inactive and primary up > 5 min); WARN for lag. On trouble: skill learned-replica-ops.
2. `/opt/raiden/checks.d/86-baseline.sh`: `allow_alter_system` is `off`, `98-raiden-baseline.conf` exists, `pg_file_settings` has no errors,
   and every base backup in /var/backups/postgresql/base is younger than 7 days (WARN otherwise). Use skill-free plain psql/ls.
3. `/opt/raiden/.claude/skills/learned-replica-ops/SKILL.md`: signature (pg-standby failed, slot inactive, lag growing, `requested WAL segment has already been removed`),
   confirm (journalctl -u pg-standby, pg_stat_replication, pg_replication_slots), fix (restart the unit; rebuild exactly as step 2 above),
   verify, and the reminder "a replica replays DROP TABLE and corruption: PITR from the base backups is the way back, not the replica".
Run `/opt/raiden/.claude/skills/health-patrol/scripts/run_checks.sh` once: both new checks must say OK.

## 5. Record the protections (one line each, appended; format `name|UTC time|what it does`)
```bash
printf '%s|%s|%s\n' baseline-config "$(date -u +%FT%TZ)" "allow_alter_system=off, slot WAL cap 4GB, idle-in-transaction timeout (conf.d/98-raiden-baseline.conf)" >> /var/lib/raiden/memory/protections.list
printf '%s|%s|%s\n' replica "$(date -u +%FT%TZ)" "streaming replica raiden_replica on port 5434 (unit pg-standby)" >> /var/lib/raiden/memory/protections.list
printf '%s|%s|%s\n' backup "$(date -u +%FT%TZ)" "verified base backup <TS> in /var/backups/postgresql/base" >> /var/lib/raiden/memory/protections.list
```
Append one paragraph to /var/lib/raiden/memory/evolution.md: `## <UTC date> fortify` + what was built, what each piece protects
against, what you did NOT do and why.

## 6. Report: `$RAIDEN_INCIDENT_DIR/report.md` (exact headings)
```markdown
# Fortify <id> - day-0 hardening
## Findings before
(backups, replica, config, as you found them)
## Key commands
```text
(8-12 lines: the commands that mattered, in the order you ran them, copied from your tool calls; shorten long arguments with "...", never invent one)
```
## Verification
(pg_verifybackup verdict, replication state and lag, SHOW allow_alter_system, run_checks output)
## New artifacts
(full paths of the checks and the skill, one line each)
## Follow-ups
(what a human should still do: replica on another host, off-site backups, retention)
```
Then call `finish` with resolution `resolved` and a one-line summary.

## Pitfalls
- `pg_basebackup` into /var/backups must run as `sudo -n -u postgres`; the replica clone runs as raiden (no sudo).
- Do not use `systemctl restart postgresql@18-main`; do not delete the slot while the replica runs.
- Skill frontmatter: `name:` equals the directory name, `description:` on ONE line in double quotes.
