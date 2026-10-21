---
name: learned-extension-guard
description: "CREATE EXTENSION pg_surgery, raiden protection: extension is not on the allowlist, or CRIT extension-guard - confirm unsafe extension and TOAST surgery evidence without weakening the guard."
---
# learned-extension-guard

## Signature
- Log: `CREATE EXTENSION pg_surgery`, often followed quickly by `DROP EXTENSION`.
- Data: `unexpected chunk number 2 (expected 1)` or missing TOAST chunks.
- Guard: `raiden protection: extension "pg_surgery" is not on the allowlist` (42501).
- Patrol: `CRIT extension-guard` or `WARN extension-guard`.

## Confirm (read-only)
```bash
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT extname FROM pg_extension; SELECT * FROM raiden.allowed_extensions; SELECT evtname,evtenabled,evtevent,evtfoid::regprocedure,evttags FROM pg_event_trigger;"
bash /opt/raiden/checks.d/87-extension-guard.sh
jq -Rr 'fromjson? | select((.message//"")|test("pg_surgery|chunk number|raiden protection:")) | [.timestamp,.user,.message,.statement] | @tsv' /var/log/postgresql/raiden-*.json | tail -30
```
Expected: only amcheck/plpgsql, matching registry; guard enabled and correct binding.
Rejected CREATE alone is successful prevention, not proof of existing corruption.

## Fix
1. Load pg-triage; if TOAST symptoms, load toast-corruption and data-loss-verification.
2. Preserve logs, catalog definitions and patrol output in the incident directory.
3. If guard blocked creation and catalogs are intact, do not change SQL or restart.
4. If drift, an unsafe extension, or suspicious live surgery is confirmed, identify the
   actual culprit session; use the matching incident runbook to stop only that culprit.
5. Escalate any requested removal/disablement of the protection or allowlist expansion.
   Do NOT rerun a seed-from-installed-extensions installer after suspected compromise.
6. For damaged payloads, use a pre-event PITR SIDE clone, hash-validate each value,
   then atomic keyed UPDATEs per toast-corruption; never delete damaged rows.

## Verify
```bash
bash /opt/raiden/checks.d/87-extension-guard.sh
bash /opt/raiden/checks.d/90-protections.sh
bash /opt/raiden/.claude/skills/preventive-protections/scripts/prove.sh extension-allowlist
/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json
```
Expected: checks OK, PROOF OK, reconcile ok=true and lost/corrupt/unreadable=0.
For actual damage also require full row scanner and amcheck per toast-corruption.

## Pitfalls and limits
- Event trigger is per database and fires at ddl_command_end: rejected installation
  rolls back before surgery functions become available to a subsequent statement.
- A superuser can bypass or alter SQL protections; monitor access and investigate drift.
- Catalog sampling can miss rapid create/drop; existing DDL logs retain that evidence.
- Checksums may remain valid after WAL-logged tuple surgery; zero failures is not health.
- A replica replicates corruption. Target PITR just before the event, not an arbitrary
  two-minute margin which can omit recently acknowledged victim rows.
- Human-only reversal after review: `DROP EVENT TRIGGER raiden_extension_allowlist;`
  rAIden must never execute that command; approved extension changes require escalation.

## Origin
Incident inc-20261021T005104-7d61, 2026-10-21; four hash-validated payloads recovered.
