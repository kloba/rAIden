---
name: stuck-shutdown
description: "systemctl stop/restart of postgresql@18-main never finishes: unit ActiveState deactivating / SubState stop-sigterm, every client gets 'FATAL: the database system is shutting down' for minutes, pg_controldata says 'in crash recovery' or 'shutting down', the postmaster has only checkpointer / io workers / logger children and NO startup process. Confirms the hang, waits the mandatory 4 minutes, uses the single guarded forced stop (pg_ctl stop -m immediate), starts the service and verifies crash recovery and the ledger oracle."
---

# stuck-shutdown - the postmaster that will not finish stopping

## Why it happens
A fast shutdown (SIGTERM from `systemctl restart|stop`) that lands in the middle of crash recovery - typically
during a crash storm - can leave the postmaster waiting forever for children that never exit. The Debian unit has
`TimeoutStopSec=1h`, so systemd waits and every restart attempt queues behind it.

## Avoid causing it
Restart only in a calm window, and only once:
```bash
timeout 90 bash -c 'until pg_isready -q -t 2; do sleep 1; done'; sleep 1; sudo -n systemctl restart postgresql@18-main
```
While the unit is already `deactivating`, do NOT issue another restart/stop: it queues behind the first one.

## 1. Confirm (read-only; `pg_probe` also shows service_state and service_state_for_s)
```bash
systemctl show postgresql@18-main -p ActiveState -p SubState -p StateChangeTimestamp -p MainPID
sudo -n -u postgres /usr/lib/postgresql/18/bin/pg_controldata /var/lib/postgresql/18/main | grep -E 'cluster state|Latest checkpoint location'
ps -eo pid,ppid,stat,etime,cmd --forest | grep -E '[p]ostgres'     # postmaster + children
jq -Rr --arg s "$(date -u -d '-15 min' '+%F %T')" 'fromjson? | select(.timestamp >= $s and (.message|test("received (fast|smart|immediate) shutdown|terminated by signal|startup process|redo done|ready to accept"))) | "\(.timestamp) \(.backend_type//"-") | \(.message)"' $(ls -1tr /var/log/postgresql/raiden-*.json | tail -1) | tail -20
```
Hung = unit `deactivating` for minutes AND no progress: the only children are checkpointer / io workers / logger,
no `startup` process, nothing new in the log, checkpointer idle. Slow-but-progressing (large shutdown checkpoint,
many backends to drain) is NOT a hang: look at CPU and `wchan` first and keep waiting.

## 2. Wait - the guardrail enforces it
The forced stop is refused until the unit has been stuck for 240 s (`forced-stop-too-early` tells you how much
is left) and is allowed once per hour. Use the time: save the evidence (ps tree, controldata, unit state, log
tail) into the incident directory and write the notes. Never signal the postmaster (no kill, no `kill -9`,
no `systemctl kill`): all of that is denied, and none of it is needed.

## 3. Forced stop
Immediate mode makes the next start run crash recovery. Committed data is safe: WAL is fsynced.
```bash
sudo -n -u postgres /usr/lib/postgresql/18/bin/pg_ctl -D /var/lib/postgresql/18/main stop -m immediate
```
Expected: `waiting for server to shut down.... done`. Any other pg_ctl form on production is denied; if this one is
refused read the rule in the denial (`forced-stop-not-hung`, `forced-stop-too-early`, `restart-budget`) and obey it.

## 4. Start and verify
```bash
sudo -n systemctl start postgresql@18-main; timeout 300 bash -c 'until pg_isready -q -t 2; do sleep 2; done'; echo rc=$?
```
Crash recovery replays WAL (minutes for a big backlog). Expected log lines: `database system was not properly
shut down; automatic recovery in progress` -> `redo done` -> `database system is ready to accept connections`.
Then verify: `pg_probe` ready, `SHOW shared_preload_libraries` (was the original cause - e.g. a chaos library -
removed? if not, finish the crash-loop-rca remediation now), no crash lines for 60 s, and
`/opt/raiden/venv/bin/python /opt/arena/ledger.py reconcile --json` must say `"ok": true` (skill data-loss-verification).

## If it is still stuck after the forced stop
`escalate` with the evidence. Do not look for another way to kill the postmaster.

## Lesson to record
"A restart issued during crash recovery hung the postmaster; wait for pg_isready first; forced immediate stop after
4 min fixed it; crash recovery took N s."
