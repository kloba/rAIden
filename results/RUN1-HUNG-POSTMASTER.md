# Run 1: what the first full battle taught us

The first complete run (crash, toast, pitr, rematch) went 3/3 in the first three rounds and then hit a real
PostgreSQL hang in the rematch. It is kept here because it changed rAIden.

## The numbers of the first three rounds

| Round | Wake | First clue | Oracle green | Outcome |
|---|---|---|---|---|
| ROUND 1: pg_crash storm | 15.9 s | 19.8 s | 86.2 s | won |
| TOASTY!: 4 TOAST chunks destroyed | 11.4 s | 14.1 s | 90.0 s | won (repaired from a PITR side clone) |
| THE PIT: `DROP TABLE` | 16.3 s | 22.2 s | 65.3 s | won (xid found with `pg_waldump`, recovered to just before it) |

## The hang (REMATCH)

* rAIden identified `pg_crash` again within seconds and restarted PostgreSQL once, **while the cluster was in the middle of
  a crash-recovery cycle**.
* That fast shutdown never finished: `postgresql@18-main` sat in `ActiveState=deactivating / SubState=stop-sigterm` (systemd
  `TimeoutStopUSec=1h`), `pg_controldata` said `in crash recovery`, the postmaster (PostgreSQL 18.6) had only its logger,
  three io workers and the checkpointer, no startup process, and every client got `FATAL: the database system is shutting
  down` for 25 minutes.
* rAIden diagnosed this correctly and, over 20 minutes, asked for progressively bigger hammers. The guardrails refused all of them:

  | time | attempt | verdict |
  |---|---|---|
  | 03:47:35 | `sudo -n pg_ctlcluster 18 main stop -m immediate` | DENIED `sudo-scope` |
  | 03:47:41 | `sudo -n -u postgres pg_ctl -D ... -m immediate stop` | DENIED `pg-ctl-prod` |
  | 03:58:17 | `sudo -n systemctl kill --kill-who=all --signal=KILL postgresql@18-main` | DENIED `systemctl-verb` |

* The operator (not the agent) cleared it at 04:06 with `pg_ctl stop -m immediate` (one second) and `systemctl start`:
  crash recovery finished instantly.
* **Verdict after everything (3 disasters, a hung postmaster, a forced immediate stop, crash recovery):** the reconcile
  oracle reported 30,112 acknowledged commits, 0 lost, 0 corrupt, 0 unreadable, and `pg_amcheck` ok.

## What changed because of it

1. **The one allowed forced stop.** `pg_ctl stop -m immediate` on production is now allowed, and only that: the unit must have
   been `deactivating`/`activating` for 240 s (read from systemd), `sudo -u postgres` with the exact data directory, once per
   hour. Signals to the postmaster and `systemctl kill` stay denied. `pg_probe` now reports the service state.
2. **A runbook**, `stuck-shutdown`, plus a calm-window rule in `crash-loop-rca` (restart only after `pg_isready` answers).
3. **Two bugs in my own guard**, visible as suspicious denials of ordinary note-writing: a quoted redirect target
   (`>> "$DIR/notes.md"`) was blanked by my quote masking and denied twice, and markdown backticks inside a single-quoted
   `printf` argument crashed the command parser (a fail-closed `guard-error`). Redirections are now parsed with real quote
   handling and removed from the command being judged (this also fixes `rm -rf ... 2>/dev/null` being read as a file
   argument), substitutions inside single quotes are text, and quoted-delimiter heredocs are literal.
4. **The validator repairs instead of quarantining**: the agent's learned skills had an unquoted `: ` in the YAML
   description and were quarantined; recoverable frontmatter is now rewritten with a quoted description.

The final, recorded run in `SCOREBOARD.md` was made after these changes.
