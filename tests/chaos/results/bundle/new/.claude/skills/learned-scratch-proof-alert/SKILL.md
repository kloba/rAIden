---
name: learned-scratch-proof-alert
description: "raiden-p5-scratch-proof, scratch_p5_migrator still waiting for AccessExclusiveLock, or CRIT scratch-proof-guard - distinguish expected proof timeouts from production locks and verify primary refusal."
---
# learned-scratch-proof-alert
## Signature
- user scratch_p5_migrator, application_name raiden-p5-scratch-proof, database ledger.
- Exact statement LOCK TABLE public.scratch_p5_queue_probe IN ACCESS EXCLUSIVE MODE.
- Expected wait then 55P03 at 3s; scratch analyst idle timeout at 15s.
- CRIT scratch-proof-guard means script fingerprint drift or leaked scratch objects/sessions.
## Confirm
```bash
bash /opt/raiden/checks.d/89-scratch-proof-guard.sh
bash /opt/raiden/checks.d/89-lock-queue.sh
PGCONNECT_TIMEOUT=3 psql -X -d ledger -c "SELECT pid,usename,application_name,state,pg_blocking_pids(pid),query FROM pg_stat_activity WHERE datname='ledger'; SELECT to_regclass('public.scratch_p5_queue_probe');"
```
- Correlate exact log PID/time/user/application/statement with trusted tool audit and saved proof output.
- Incident inc-20261021T012504-bb45 had PIDs 49135/49258, relation OIDs 158535/159264, 01:22 UTC.
- These historical IDs are evidence only, never a standing allowlist.
- Expected healthy results: detectors OK, scratch roles/table absent, no blocked production clients.
## Fix
1. If exact trusted proof provenance and cleanup verified, record expected protection behavior; no DB intervention.
2. If actual production chain exists, load learned-lock-queue; cancel only verified culprit as that runbook specifies.
3. If script drift/residue exists, preserve file/catalog evidence; inspect before changing or deleting anything.
4. Do not automatically accept a new hash baseline. Restore guard only from reviewed saved source, after copying current file.
5. Legitimate further runtime proof requires a disposable separately initialized cluster, not production/replica/PITR clone.
## Verify
```bash
# Original incident-producing invocation now exits 1 before any connection/writes.
timeout 10 /opt/raiden/venv/bin/python /var/lib/raiden/work/prove_lock_queue_defaults.py
# Explicit primary target also exits 1, with primary system identifier refusal.
RAIDEN_PROOF_HOST=/var/run/postgresql RAIDEN_PROOF_PORT=5432 RAIDEN_PROOF_DBNAME=ledger timeout 10 /opt/raiden/venv/bin/python /var/lib/raiden/work/prove_lock_queue_defaults.py
bash /opt/raiden/checks.d/90-protections.sh
```
- Check no new scratch roles/table or proof connections; pg_probe unchanged and healthy.
- Run ledger reconcile if any production transactions/data were affected.
## Pitfalls
- Application name/user/SQL text alone is spoofable: never suppress alerts based on these alone.
- No logging thresholds, watcher rules, P5 policies or production lock checks are weakened.
- Guard covers this helper only, not manual SQL/catalogue helpers, privileged edits or copies of old scripts.
- System identifier rejects physical clones/replica too; create an independent disposable test cluster with no entries table.
- Fresh proof requires equivalent role policy and suitable authenticated TCP target; no production credentials or auth edits.
- WARN on unreachable DB is not proof of cleanup; retry after readiness.
## Origin
Incident inc-20261021T012504-bb45, 2026-10-21: successful scratch P5 proof woke watcher; no production impact.
