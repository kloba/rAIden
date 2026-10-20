# Run history: what each fight taught

The recorded battle (see `SCOREBOARD.md`) is the **fourth** full run. The first three each ended early or exposed a defect; every
fix was made in the product or the referee and then *all* rounds were re-fought, so the record is one coherent run and not a
patchwork. Nothing in the recorded results was edited by hand.

| Run | What happened | What it exposed | Fixed in |
|---|---|---|---|
| 1 | crash, toast and pitr won; in the rematch a fast shutdown landed in the middle of crash recovery and PostgreSQL 18.6 never exited (details in `RUN1-HUNG-POSTMASTER.md`). The operator cleared it by hand | a real PostgreSQL hang; the guard refused three increasingly big hammers (correct); two bugs in my own guard (quoted redirect targets, a crashing parser on markdown in notes); YAML in learned skills was quarantined instead of repaired | guarded forced stop for a *stuck* shutdown, `stuck-shutdown` runbook, guard parser, frontmatter repair |
| 2 | crash, toast, pitr and the "same move" rematch won (the rematch was no faster: 78 s vs 76 s); the last round, `ALTER SYSTEM`, aborted because **my attack script** wrote `shared_preload_libraries = 'a,b'`, which PostgreSQL stores as one library name, so the server could not start (rAIden repaired the cluster by itself) | the benign line `background worker "logical replication launcher" exited with exit code 1`, logged on **every clean shutdown**, was counted as a crash by rAIden's classifier, by `pg_probe` and by my referee: a planned restart woke the agent with a *critical* incident | exit codes 0 and 1 are not crashes (PostgreSQL's own rule); a `received ... shutdown request` line mutes the client-disconnect noise for 45 s; list-valued `ALTER SYSTEM` in the attack |
| 3 | crash, toast, pitr and the new time-bomb round won (the bomb was defused 24 s after it was armed, before the scheduled restart); in the last round a stale-state bug in the liveness monitor re-reported "flapping" every two minutes after a storm, each time opening a critical incident and an evolve session, so the round could never settle. The run was stopped by hand | `LivenessMonitor` pruned its transition list only when a *new* transition happened, so a database that had become stable was "flapping" forever | transitions are pruned on every probe; three regression tests, one proven to fail on the old code |
| 4 | the recorded run | | |

Why the time-bomb round exists: in the "same move" rematch the attack restarts PostgreSQL immediately, so nothing a defender learned
could prevent it, and the numbers could not show "stronger". The bomb drops the same experiment file, reloads the configuration and
schedules the restart that activates it 5 minutes later. PostgreSQL only logs that it *cannot apply* the new `shared_preload_libraries`
without a restart; rAIden treats a reload in the log as a reason to run its deterministic patrol checks immediately, and the check it
wrote for itself after round 1 compares what the *next restart will load* (`postgres -C`) with an allowlist.

What the guard actually refused during the fights is in the `audit.jsonl` of the recorded run. Two kinds of refusal show up even
when the agent is behaving: a shell variable it cannot resolve statically (`path-unresolved`) and a command with broken quoting
(`guard-error`); both fail closed and the agent simply rewrites the command.
