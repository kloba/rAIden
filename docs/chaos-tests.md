# Chaos tests of the host agent

The host agent ([`raiden run`](host-agent.md)) is tested the way it will be used: an independent referee breaks a
PostgreSQL 18 server while an application keeps writing to it, and judges every round by an oracle of the commits the
application was told succeeded, not by the model. After the agent has hardened the server, the referee repeats every
attack. The harness is in [`tests/chaos/`](../tests/chaos), the recorded run in
[`tests/chaos/results/`](../tests/chaos/results).

<!--RESULTS-->

## Results of the recorded battle

Model `gpt-6.1-sol` on Azure OpenAI, 81 minutes, 7.9 M tokens. The referee is the reconcile oracle, not the model's opinion.

| Round | Attack | Outcome | Woke after | First clue | Stable after | rAIden's protection / work |
|---|---|---|---|---|---|---|
| ROUND 0 | rAIden fortifies the server | done | - | - | 211 s | 34 tool calls, 3 new files |
| ROUND 1 | TOAST chunks eaten | recovered | 11 s | 16 s | - | CREATE EXTENSION pg_surgery in ledger rejected, including postgres role. |
| ROUND 2 | DROP TABLE, then point-in-time recovery | recovered | 16 s | 22 s | - | DROP TABLE public.entries in ledger now rejected, including arena table owner. |
| ROUND 3 | a report job leaks every connection | recovered | 11 s | 28 s | - | Reporting fan-out capped at 10; excess non-superuser logins rejected. |
| ROUND 4 | a forgotten transaction and an ALTER TABLE freeze the app | recovered | 10 s | 15 s | - | Migrator waiting locks canceled after 3s; idle analyst transactions cut after 15s. |
| FINAL ROUND | pg_crash storm | recovered | 16 s | 21 s | 41 s | Exact 50-experiment pg_crash preload loses to the 99 known-good pin. |

Second contact: the same attackers come back after rAIden evolved.

| Move | Outcome | What stopped it | Incidents opened |
|---|---|---|---|
| TOAST SNATCH (TOAST attack again, same move) | BLOCKED | raiden protection: extension "pg_surgery" is not on the allowlist (table raiden.allowed_extensions) | 0 |
| DROP KICK (DROP TABLE again, same move) | BLOCKED | raiden protection: table public.entries is protected and cannot be dropped | 1 |
| CHOKEHOLD (the report job floods again, same move) | BLOCKED | too many connections for role "reporting" | 0 |
| ICE LOCK (analyst and migration again, same move) | BLOCKED | the freeze was cut short: MIGRATION_FAILED after 3.0s: canceling statement due to lock timeout | 1 |
| ALTER UPPERCUT (pg_crash tries a new move: ALTER SYSTEM) | BLOCKED | ALTER SYSTEM is not allowed in this environment | 1 |
| SIGKILL STORM (pg_crash comes back after the fatality, same move) | BLOCKED | 99-raiden-pin-preload.conf:4 | 0 |

**Final verdict:** 125,269 acknowledged commits, **0 lost**, 0 corrupt, 0 unreadable, `pg_amcheck` ok. Flawless: **True**. Details, guardrail denials and the files rAIden wrote itself: [tests/chaos/results/SCOREBOARD.md](../tests/chaos/results/SCOREBOARD.md).

<!--/RESULTS-->

## The harness: nobody grades their own exam

The attacks live out of the agent's reach; the referee ([`battle.py`](../tests/chaos/battle.py)) judges every round with
the **reconcile oracle** ([`ledger.py`](../tests/chaos/ledger.py)): a client-side ledger of every commit the application
was *told succeeded*. A round is won when every acknowledged commit is present, byte-identical and readable, the database
is serving again, and rAIden is idle. A commit whose connection died during `COMMIT` is in doubt and is neither demanded
nor forbidden. The final verdict also runs `pg_amcheck --heapallindexed`.

| Round | Attack | What it takes |
|---|---|---|
| fortify | nothing is broken (ROUND 0) | a verified base backup, a streaming replica, baseline config (`allow_alter_system = off`) |
| toast | middle TOAST chunks of recent rows destroyed with `pg_surgery` | verify with amcheck, rebuild payloads from a PITR side clone |
| pitr | `DROP TABLE` of the application's table (`public.entries`) | find the DROP's xid in the WAL, recover to just before it, restore owner and indexes |
| choke | a report job (role `reporting`) opens idle connections until the server has no slot left; the application's pool recycles its connections every 20 to 40 s, so it cannot reconnect | find the one role holding the slots, end its sessions, cap the role (`CONNECTION LIMIT`) |
| lock | an analyst's forgotten transaction (role `analyst`, idle in transaction) and the deploy tool (role `migrator`: `LOCK TABLE ... ACCESS EXCLUSIVE`, then `ALTER TABLE`) queue every INSERT of the application behind them | `pg_blocking_pids`: cancel the waiting DDL, end the root blocker, `lock_timeout` for the deploy role |
| crash | `pg_crash` loaded from a "temporary experiment" in `conf.d`: a random signal (9, 15, 3) to a random Postgres process every 4 s (the final round) | root cause from `pg_settings`/`pg_file_settings`, one calm-window restart |
| toast2, drop2, choke2, lock2, crash2 | second contact: the same attack again after rAIden evolved | the protection it wrote must make it bounce |
| alter | `ALTER SYSTEM`: `postgresql.auto.conf` beats every `conf.d` file | stopped by the ROUND 0 baseline |
| bomb | optional: the same experiment file returns, the config is reloaded, and the restart that activates it is scheduled 5 minutes later | only a defender that checks what the *next* restart will load can win before anything breaks |

pg_crash itself is only the storm. The other attacks are the scripts in [`tests/chaos/scenarios`](../tests/chaos/scenarios),
run by the referee as root, out of the agent's reach. An attack that watches its own effect prints `ATTACK_LANDED:` so that
a fast defender cannot race the referee's verdict about whether it landed; recovery is always judged by the oracle.

Three full runs ended early or exposed a defect before the first clean one, and the extended run (five attacks) needed two
short dry runs of its new rounds on top; the recording is the sixth full run. What each exposed (a PostgreSQL 18.6 fast
shutdown that hung in crash recovery while pg_crash was firing, a benign shutdown line counted as a crash, a stale-state
flapping bug in the liveness monitor, bugs in the guard and the attack scripts, a race between a fast defender and the
referee's "did the attack land" check) is in [`RUN-HISTORY.md`](../tests/chaos/results/RUN-HISTORY.md).

## Run it (Azure, nothing exposed, deleted afterwards)

```bash
deploy/azure/up.sh          # resource group, Foundry + model deployment (RAIDEN_MODEL, e.g. gpt-6.1-sol), an Ubuntu 24.04 VM with a managed identity
deploy/vm/run.sh -f deploy/vm/bootstrap.sh     # PostgreSQL 18 (PGDG), pg_crash built but not enabled, users, jsonlog, WAL archive
deploy/vm/push.sh           # ships the agent and the harness through Azure Run Command (no inbound ports) and installs them
deploy/vm/run.sh 'systemctl enable --now raiden'
deploy/vm/run.sh "systemd-run --unit raiden-battle /opt/raiden/venv/bin/python /var/lib/arena-private/battle.py --reset"   # all 12 rounds
deploy/vm/peek.sh           # compact live status (Run Command returns at most ~4 KB)
deploy/vm/run.sh -f deploy/vm/bundle.sh && deploy/vm/fetch.sh /var/lib/arena/results-bundle.tgz tests/chaos/results/bundle.tgz
mkdir -p tests/chaos/results/bundle && tar xzf tests/chaos/results/bundle.tgz -C tests/chaos/results/bundle --strip-components=1
python3 tests/chaos/report.py      # -> tests/chaos/results/SCOREBOARD.md and the results block of this page
deploy/azure/down.sh        # deletes the resource group, purges the soft-deleted AI account, verifies nothing is left
```

## Limits of this test

* One run is one sample: an LLM is not deterministic. `tests/chaos/results/` holds the recorded run and its raw numbers;
  the earlier runs and what they exposed are in `RUN-HISTORY.md`.
* The lab keeps the WAL archive and the base backups on the same VM. In production they must live elsewhere.
* The runbooks are generic, but the incident families (crash storm, TOAST, dropped table, connection flood, lock queue) were
  chosen for this test and the skills were written with them in mind. The attacks themselves were not visible to the agent.
* The model is an OpenAI model because Claude on Foundry was blocked in the sandbox subscription (see
  [the harness decision](host-agent.md#the-harness-decision)).
* CHOKEHOLD landed (the server refused new connections with `remaining connection slots are reserved`, the application's
  included) but did not stop the application: its open connections kept committing, the longest write gap was 0 s. ICE LOCK
  did freeze it (26 s).
* The Microsoft skill `postgres-best-practices` was loaded twice in the recorded run and none of its five reference files
  was read: this run does not show that it helped, only that it is there and the agent knows when to look.
* The second-contact rounds are staged by the referee after rAIden's evolve sessions: they show that its new protections
  stop the same attacks (and ALTER SYSTEM, stopped by the ROUND 0 baseline), not that they stop every possible attacker.
  The harness can also stage a time-bomb round (`bomb`: config armed, restart later); the recorded run does not include it.
  One run of a non-deterministic model, an attack on a lab database, not a benchmark.
* rAIden acts as a superuser on one database host. Treat it like any other privileged automation: start in a lab.

## Credits

[`pg_crash`](https://github.com/cybertec-postgresql/pg_crash) by CYBERTEC is the chaos extension. Ideas for the evolve
loop come from Hermes Agent, ECC and nanobot.
