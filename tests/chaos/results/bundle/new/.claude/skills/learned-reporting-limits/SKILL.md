---
name: learned-reporting-limits
description: "FATAL 53300 remaining connection slots are reserved, too many connections for role reporting, or CRIT reporting-limits - confirm reporting fan-out or role-policy drift and safely restore service."
---
# learned-reporting-limits
## Signature
- FATAL SQLSTATE 53300: remaining connection slots are reserved or too many clients already.
- A burst of idle reporting clients with empty application_name starves arena logins.
- `too many connections for role "reporting"` means cap admission works, not global exhaustion.
- WARN/CRIT reporting-limits reports cap, timeout, privilege, database override or occupancy drift.
## Confirm (read-only)
```bash
bash /opt/raiden/checks.d/89-reporting-limits.sh
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT rolname,rolsuper,rolcanlogin,rolconnlimit,rolconfig FROM pg_roles WHERE rolname='reporting'; SELECT setdatabase,setrole,setconfig FROM pg_db_role_setting WHERE setrole=0 OR setrole=(SELECT oid FROM pg_roles WHERE rolname='reporting');"
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT usename,state,count(*),min(backend_start),max(now()-state_change) FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY 1,2 ORDER BY 3 DESC; SELECT pid,usename,state,pg_blocking_pids(pid) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0;"
```
Expected reporting NOSUPERUSER, LOGIN, limit 10, timeouts 2min/15s, no database timeout overrides.
Use pg_probe; if not a reporting storm, load connection-storm and follow the actual culprit.
## Fix
1. Preserve protections; record evidence and proposed change in incident notes first.
2. If legitimate workload hits the cap, escalate to client owner for pool bounds/backoff/application_name.
   Never raise max_connections or relax a registered cap just to silence an alert.
3. If the role became superuser or database overrides appeared, escalate privilege/config drift;
   do not improvise privilege changes or disable guards. Confirm who altered it from DDL logs.
4. When unlimited reporting is confirmed and restoration is authorized, save current catalog first:
```bash
PGCONNECT_TIMEOUT=3 psql -X -At -d postgres -c "SELECT rolname,rolsuper,rolconnlimit,rolconfig FROM pg_roles WHERE rolname='reporting'" > "$RAIDEN_INCIDENT_DIR/reporting-role.before.txt"
PGCONNECT_TIMEOUT=3 psql -X -v ON_ERROR_STOP=1 -d postgres -c "ALTER ROLE reporting CONNECTION LIMIT 10; ALTER ROLE reporting SET idle_session_timeout='2min'; ALTER ROLE reporting SET idle_in_transaction_session_timeout='15s';"
```
5. Review existing sessions. Only if reporting idle clients exhaust slots, disconnect precisely those:
```bash
PGCONNECT_TIMEOUT=3 psql -X -d postgres -c "SELECT pid,state,pg_terminate_backend(pid,5000) FROM pg_stat_activity WHERE backend_type='client backend' AND usename='reporting' AND pid<>pg_backend_pid() AND NOT (SELECT rolsuper FROM pg_roles WHERE rolname='reporting') AND state IN ('idle','idle in transaction','idle in transaction (aborted)');"
```
Idle-in-transaction termination rolls back uncommitted work; obtain evidence before using it.
No restart/reload required: role defaults apply at LOGIN, not to existing pools or SET ROLE.
## Verify
- Run the detector and 90-protections.sh; pg_probe should be ready, below capacity, unchanged start.
- Observe >2min; check no new global 53300, writers advancing and no blocked sessions.
- After write failures/terminated transactions run ledger.py reconcile --json; require ok=true.
- Optional calm-window proof: `timeout 45 /opt/raiden/venv/bin/python /var/lib/raiden/work/prove_reporting_role_limits.py`.
  It opens ten ephemeral non-superuser scratch logins, rejects the eleventh, verifies fresh defaults,
  closes every test connection and removes only its scratch role. No reporting credentials needed.
## Pitfalls and scope
Connection limits are approximate under racing logins, cluster-wide and exempt superusers.
Timeouts are user-settable; DB-role overrides and old/session-modified settings can defeat defaults.
The detector flags any database timeout override for review (even a stricter one); never auto-reset it.
The proof tests equivalent scratch admission, not actual reporting credentials or timeout expiry.
Caps protect against this role's fan-out, not many uncapped roles or client pooling defects.
Legitimate policy revisions require human review; rAIden never weakens recorded protections.
## Origin
Incident inc-20261021T010712-42b2, 2026-10-21: 92 idle reporting logins, 98/100 clients;
cap and targeted idle disconnect restored arena writes without restart or acknowledged-commit loss.
