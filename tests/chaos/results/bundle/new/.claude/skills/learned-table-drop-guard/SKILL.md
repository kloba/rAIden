---
name: learned-table-drop-guard
description: "raiden protection: table public.entries is protected and cannot be dropped; WARN/CRIT ddl-attempts, CRIT table-guard or relation entries does not exist - correlate rejected DDL, confirm guard drift and recover safely."
---
# learned-table-drop-guard
## Signature
- `ERROR: raiden protection: table public.entries is protected and cannot be dropped` (42501): rejected destructive DDL, not data loss.
- `CRIT table-guard`: missing table/registry, unregistered application table, changed trigger/function/owner or arena write privilege.
- `relation "entries" does not exist` (42P01): treat as actual data-affecting incident until proven otherwise.
## Confirm
1. Load pg-triage and probe primary readiness before SQL.
2. Run read-only independent detector:
   `bash /opt/raiden/checks.d/88-table-guard.sh`
   Expected OK with entries present, registered, guard and permissions intact.
3. Inspect registry, trigger and table:
   `PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT to_regclass('public.entries'); SELECT * FROM raiden.protected_tables; SELECT evtname,evtevent,evtenabled,evtfoid::regprocedure FROM pg_event_trigger;"`
4. Attribute attempts using existing DDL logs:
   `jq -Rr 'fromjson? | select(.dbname=="ledger" and ((.message // "") | test("DROP TABLE|DROP SCHEMA|raiden protection"))) | [.timestamp,.user,.pid,.message] | @tsv' /var/log/postgresql/raiden-20261021.json`
   Choose the current dated log, not necessarily this origin-date file.
## Fix
- If guard rejected DROP and table exists, do NOT restore data: inspect client and failed migration; escalate legitimate DDL that protection blocks.
- Never delete registry rows, disable/drop triggers, alter the protected function or reseed a drift baseline to silence an alert.
- Guard drift: compare saved baseline, live function definition, trigger binding and role permissions. Escalate suspected tampering if repair would weaken existing protection.
- New legitimate application table with intact guard: inspect ownership/schema first, then register its exact schema/name as superuser:
  `PGCONNECT_TIMEOUT=3 psql -X -d ledger -v ON_ERROR_STOP=1 -c "INSERT INTO raiden.protected_tables(schema_name,table_name) VALUES ('public','reviewed_new_table') ON CONFLICT DO NOTHING;"`
  Independently review all catalog differences before backing up and updating baseline.
- Actual table absent: load pitr-restore and data-loss-verification; identify destructive commit XID using DDL log plus WAL relation-file deletion records.
- Recover into SIDE CLONE stopping exclusively before that XID; inspect last acknowledged rows, ownership, ACL, hashes and indexes.
- Table-only custom dump restore must be atomic; never overwrite production PGDATA or rewind all databases.
- No primary restart is needed for this SQL protection or table restoration.
## Verify
- Run 88-table-guard.sh and 90-protections.sh; both must be OK.
- Catalogue safe proof uses scratch table only:
  `bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove.sh protected-tables`
- Owner-role proof: create/register scratch table inside BEGIN, ALTER its owner to arena, SET LOCAL ROLE arena, issue scratch DROP in a separate psql -c, then ROLLBACK; expect protection error and no scratch objects/registry entries left.
- Never test DROP against real entries, even in a rollback transaction: it takes destructive locks on the live application.
- After recovery: run pg_amcheck and `/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json`; require ok=true and lost/corrupt/unreadable=0.
## Scope and pitfalls
- Registry matches schema/name, not OID; RENAME/SET SCHEMA can bypass it. The detector alerts missing registered or new unregistered application tables, but is not synchronous rename prevention.
- sql_drop blocks registered table removal including schema CASCADE; it does NOT prevent TRUNCATE, DELETE, destructive ALTER or DROP DATABASE.
- New/restored tables require explicit registration. Do not automatically trust current catalog state after an alert.
- Superusers can bypass SQL guards; privilege/access review and off-host backups remain necessary.
- Streaming replica replicates DROP and is NOT a backup.
- After inc-20261021T013448-3b98: a `LOG statement: DROP TABLE public.entries` is a start, never commit evidence. Run `bash /opt/raiden/checks.d/88-ddl-attempts.sh`: WARN keeps rejected attempts visible; CRIT means no matching rejection, NOT proven loss. It matches exact unquoted SQL only, same session_id/PID and error within 5s, sampling last 2MiB of each of two logs over 10min; variants/high volume can be missed. Keep independent 88-table-guard.sh enabled.
- Confirm this incident with `jq -Rr 'fromjson? | select(.pid==55221 and .session_id=="6ad816b1.d7b5") | [.timestamp,.error_severity,.state_code,.message,.statement] | @tsv' /var/log/postgresql/raiden-20261021.json`: LOG then ERROR 42501 after 3ms. New alerts require their own current log/session attribution, table/guard checks and oracle if data may be affected. Never suppress by application_name alone.
- Safe P3 reproof improvement: create/register an arena-owned uniquely named scratch table inside BEGIN, SET LOCAL ROLE arena, use a separate `-c` for DROP and final `-c ROLLBACK`. Expect 42501 plus no table/registry residue. This avoids the catalogue helper's committed scratch registry DELETE cleanup; never edit real registry rows or live entries. Log reproof provenance because expected errors can wake the watcher.
- Incident exclusive XID 88645 preserved concurrent commits that a coarse time margin would have omitted.
## Origin
Incident inc-20261021T005838-7552, 2026-10-21; arena table owner dropped entries despite intact extension-only allowlist.
