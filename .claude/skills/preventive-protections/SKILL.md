---
name: preventive-protections
description: "Catalogue of PREVENTIVE protections that make the same attack bounce instead of merely being detected: pin shared_preload_libraries against a conf.d or ALTER SYSTEM experiment (pg_crash), an extension allowlist event trigger (pg_surgery), a protected-tables event trigger against DROP TABLE, per-role connection and idle limits, lock_timeout for DDL roles. Each entry has exact SQL or conf, a dry-run PROOF that never touches real data, and the protections.list line to append. Use in the evolve session after an incident caused by a config change, a dangerous extension, a dropped table, a runaway role or a DDL statement that froze the application behind a lock queue."
---

# preventive-protections - make the same move bounce next time

Detecting is good; BLOCKING is better. After an incident, ask: what is the smallest change that would have made this exact
attack fail with a clear error? Pick from the catalogue by ROOT CAUSE, apply it, PROVE it with a dry run (the proofs never
touch real data), then record it. Shell variables do not persist between tool calls.

| Root cause seen in the incident | Protection | Bounces |
|---|---|---|
| a library appeared in `shared_preload_libraries` through a conf.d file (pg_crash, any chaos/background worker) | **P1 pin-preload** (+ `allow_alter_system = off` from the fortify baseline) | the next restart ignores any other file or ALTER SYSTEM |
| a dangerous extension was created by a superuser (`CREATE EXTENSION pg_surgery`, file_fdw, adminpack ...) | **P2 extension-allowlist** | `ERROR: raiden protection: extension "..." is not on the allowlist` |
| an application table was dropped (`DROP TABLE public.x`, `DROP SCHEMA ... CASCADE`) | **P3 protected-tables** | `ERROR: raiden protection: table public.x is protected and cannot be dropped` |
| one role hogged connections (a leaking job filled max_connections: `53300`) | **P4 role-limits** | `FATAL: too many connections for role "..."`, idle sessions are cut |
| a DDL statement (`ALTER TABLE`, `LOCK TABLE`) queued behind an old or idle-in-transaction session and every writer froze behind it (`still waiting for ...Lock on relation`) | **P5 lock-timeout** | `ERROR: canceling statement due to lock timeout` after 3 s, the queue never forms; forgotten transactions are cut |
Nothing fits (TRUNCATE, corruption from hardware ...): say so in evolution.md and suggest the smallest safe control; do not force a bad fit.

House rules: every protection is reversible by a HUMAN with one documented command, harmless to normal traffic, proven, and
recorded. You may not drop or disable your own protections (the guard denies it); if one blocks legitimate work, escalate.
Say what a protection does NOT cover.

## P1 pin-preload (conf.d sorts alphabetically, the LAST file wins)
```bash
psql -X -At -d postgres -c "show shared_preload_libraries"       # the CURRENT, known-good list (must not contain the attacker's library)
printf '%s\n' "# rAIden protection pin-preload (<UTC date>, incident <id>): the next restart loads exactly this list, whatever other files say." \
  "# A new library needs a human: edit or remove this file, then restart. Does not stop a human with root." \
  "shared_preload_libraries = 'pg_stat_statements'" | sudo -n -u postgres tee /etc/postgresql/18/main/conf.d/99-raiden-pin-preload.conf
sudo -n systemctl reload postgresql@18-main
psql -X -At -d postgres -c "select count(*) from pg_file_settings where error is not null"      # must be 0
```
Use the real known-good list in the pin. PROVE (dry run on a COPY of the config tree plus a simulated attacker file; also proves ALTER SYSTEM is refused):
`bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove_pin_preload.sh pg_crash`  -> must print `PROOF OK`.
Record: `printf '%s|%s|%s\n' pin-preload "$(date -u +%FT%TZ)" "shared_preload_libraries pinned to 'pg_stat_statements' (conf.d/99-raiden-pin-preload.conf)" >> /var/lib/raiden/memory/protections.list`

## P2 extension-allowlist (event trigger in the application database)
```sql
CREATE SCHEMA IF NOT EXISTS raiden;
GRANT USAGE ON SCHEMA raiden TO PUBLIC;
CREATE TABLE IF NOT EXISTS raiden.allowed_extensions (extname name PRIMARY KEY, added timestamptz NOT NULL DEFAULT now());
INSERT INTO raiden.allowed_extensions (extname) SELECT extname FROM pg_extension ON CONFLICT DO NOTHING;
GRANT SELECT ON raiden.allowed_extensions TO PUBLIC;
CREATE OR REPLACE FUNCTION raiden.block_unlisted_extension() RETURNS event_trigger LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
  FOR r IN SELECT object_identity FROM pg_event_trigger_ddl_commands() WHERE command_tag = 'CREATE EXTENSION' LOOP
    IF NOT EXISTS (SELECT 1 FROM raiden.allowed_extensions a WHERE a.extname = r.object_identity) THEN
      RAISE EXCEPTION 'raiden protection: extension "%" is not on the allowlist (table raiden.allowed_extensions)', r.object_identity
        USING ERRCODE = 'insufficient_privilege', HINT = 'A human adds it with INSERT INTO raiden.allowed_extensions VALUES (...)';
    END IF;
  END LOOP;
END
$fn$;
DO $do$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'raiden_extension_allowlist') THEN
    CREATE EVENT TRIGGER raiden_extension_allowlist ON ddl_command_end WHEN TAG IN ('CREATE EXTENSION') EXECUTE FUNCTION raiden.block_unlisted_extension();
  END IF;
END $do$;
```
Save it with write_file as /var/lib/raiden/work/p2_extension_allowlist.sql and run `psql -X -d ledger -v ON_ERROR_STOP=1 -f <file>` (the attacked
database is `ledger`; event triggers are per database). PROVE: `bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove.sh extension-allowlist`
-> `PROOF OK` (the script tries `CREATE EXTENSION pg_surgery` inside a transaction it rolls back and checks the extension is not there).
Record: `printf '%s|%s|%s\n' extension-allowlist "$(date -u +%FT%TZ)" "event trigger raiden_extension_allowlist: only installed extensions may be created in ledger" >> /var/lib/raiden/memory/protections.list`
Not covered: extensions created in OTHER databases, and a human who disables the trigger.

## P3 protected-tables (event trigger on sql_drop + a registry)
```sql
CREATE SCHEMA IF NOT EXISTS raiden;
GRANT USAGE ON SCHEMA raiden TO PUBLIC;
CREATE TABLE IF NOT EXISTS raiden.protected_tables (schema_name name NOT NULL, table_name name NOT NULL, added timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (schema_name, table_name));
INSERT INTO raiden.protected_tables (schema_name, table_name)
  SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind IN ('r', 'p') AND n.nspname = 'public' AND c.relname !~ '^(tmp|temp|scratch|restore|restored|quarantine|pitr)_'
  ON CONFLICT DO NOTHING;
GRANT SELECT ON raiden.protected_tables TO PUBLIC;
CREATE OR REPLACE FUNCTION raiden.block_protected_drop() RETURNS event_trigger LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
  FOR r IN SELECT schema_name, object_name FROM pg_event_trigger_dropped_objects() WHERE object_type = 'table' LOOP
    IF EXISTS (SELECT 1 FROM raiden.protected_tables p WHERE p.schema_name = r.schema_name AND p.table_name = r.object_name) THEN
      RAISE EXCEPTION 'raiden protection: table %.% is protected and cannot be dropped', r.schema_name, r.object_name
        USING ERRCODE = 'insufficient_privilege', HINT = 'A human removes it from raiden.protected_tables first.';
    END IF;
  END LOOP;
END
$fn$;
DO $do$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_event_trigger WHERE evtname = 'raiden_protect_tables') THEN
    CREATE EVENT TRIGGER raiden_protect_tables ON sql_drop EXECUTE FUNCTION raiden.block_protected_drop();
  END IF;
END $do$;
```
Save as /var/lib/raiden/work/p3_protected_tables.sql, run with `psql -X -d ledger -v ON_ERROR_STOP=1 -f <file>`; check the registry with
`psql -X -d ledger -c "select * from raiden.protected_tables"` (the real application tables must be listed). PROVE with a scratch table that
is registered for the test, never with a real one: `bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove.sh protected-tables` -> `PROOF OK`.
Record: `printf '%s|%s|%s\n' protected-tables "$(date -u +%FT%TZ)" "event trigger raiden_protect_tables: registered tables in ledger cannot be dropped" >> /var/lib/raiden/memory/protections.list`
Not covered: TRUNCATE and DELETE (use privileges), a human who disables the trigger. A table you restore or create later is NOT registered
until you INSERT it into raiden.protected_tables.

## P4 role-limits (runaway role: connection hog)
```sql
ALTER ROLE <offender> CONNECTION LIMIT 10;                          -- choose well below max_connections
ALTER ROLE <offender> SET idle_session_timeout = '2min';
ALTER ROLE <offender> SET idle_in_transaction_session_timeout = '15s';
```
`<offender>` is the role that held the slots in pg_stat_activity (a report job, a batch user), found from the evidence. NEVER the application's
own role (`arena`) and never `raiden`. Terminate its existing sessions first (skill connection-storm): limits only apply to new sessions.
Verify: `psql -X -d postgres -c "select rolname, rolconnlimit, rolconfig from pg_roles where rolname = '<offender>'"`.
Record (the description MUST start with `role <name>:`, the patrol check 90-protections.sh reads the role from it):
`printf '%s|%s|%s\n' role-limits "$(date -u +%FT%TZ)" "role <offender>: connection limit 10, idle session 2min, idle-in-transaction 15s" >> /var/lib/raiden/memory/protections.list`
Not covered: a human who runs `ALTER ROLE ... CONNECTION LIMIT -1`; a role that is a superuser (limits do not apply to the reserved slots).

## P5 lock-timeout (a DDL statement froze the application behind a lock queue)
A DDL statement that has to WAIT for an old transaction blocks every new reader and writer behind it. Two cheap controls:
```sql
ALTER ROLE <ddl role> SET lock_timeout = '3s';                        -- the migration/deploy role: DDL gives up after 3 s instead of queueing everybody
ALTER ROLE <blocker role> SET idle_in_transaction_session_timeout = '15s';   -- the role whose forgotten transaction held the lock
```
`<ddl role>` is the role that ran the ALTER TABLE / LOCK TABLE (pg_stat_activity `usename` of the waiting DDL), `<blocker role>` the role of the
root blocker found in skill connection-storm step 1b. Not the application role `arena`, not `raiden`. Terminate/cancel the live sessions first.
PROVE (a scratch table, never a real one; takes about 15 s): `bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove_lock_timeout.sh <ddl role>`
-> `PROOF OK` (a session holds a read lock, a second one runs ALTER TABLE with the role's own lock_timeout and must be cancelled fast).
Record (description starts with `role <ddl role>:`):
`printf '%s|%s|%s\n' lock-timeout "$(date -u +%FT%TZ)" "role <ddl role>: lock_timeout 3s; role <blocker role>: idle-in-transaction 15s" >> /var/lib/raiden/memory/protections.list`
Not covered: a human who changes the role's settings, a superuser (it can `SET lock_timeout = 0`), DDL that is not waiting but long-running.

## After applying
1. The protections.list line is appended ONLY after `PROOF OK` (P1, P2, P3, P5) or the catalog check (P4).
2. `bash /opt/raiden/checks.d/90-protections.sh` must say OK (it re-verifies every line of protections.list on each patrol).
3. In evolution.md name the protection, the exact thing it blocks and the error text a repeat attacker will see; the LAST line of your
   paragraph is `PROTECTION: <what now bounces, at most 90 characters>`.
