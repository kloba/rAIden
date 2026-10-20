# rAIden

**The autonomous on-call DBA for PostgreSQL.** It reads the Postgres log 24/7, wakes up on trouble, finds the root cause,
fixes it on its own behind hard guardrails, proves the fix with an independent oracle, and then makes itself stronger for
the next fight.

Built for the PGConf.EU 2026 lightning talk **"pg_crash vs rAIden"** (Mortal Kombat style, 5 minutes, no live demo, so the
fight was recorded on Azure and judged by a referee that does not trust the AI's opinion).

```
ROUND 1... FIGHT!   pg_crash storm      ->  FINISH HIM!  ->  FATALITY ("FATAL:" is already in the log)
TOASTY!             TOAST corruption    ->  repaired from a point-in-time side clone
THE PIT(R)          DROP TABLE          ->  Point-In-Time Recovery, zero commits lost
REMATCH             a time bomb (config armed, restart later), then a new move (ALTER SYSTEM)  ->  rAIden is stronger
FLAWLESS VICTORY    every acknowledged commit still there, byte for byte
```

<!--RESULTS-->

## Results of the recorded battle

Model `gpt-5.5` on Azure OpenAI, 34 minutes, 3.5 M tokens. The referee is the reconcile oracle, not the model's opinion.

| Round | Attack | Outcome | Woke after | First clue | Oracle green |
|---|---|---|---|---|---|
| ROUND 1 | pg_crash storm | won | 24 s | 28 s | 96 s |
| TOASTY! | TOAST chunks eaten | won | 11 s | 16 s | 90 s |
| THE PIT | DROP TABLE -> point-in-time recovery | won | 16 s | 22 s | 86 s |
| REMATCH | the time bomb: config armed, restart later | prevented before it bit | 4 s | 11 s | - |
| BOSS RETURNS | pg_crash returns (new move: ALTER SYSTEM) | won | 25 s | 28 s | 98 s |

**Final verdict:** 51,769 acknowledged commits, **0 lost**, 0 corrupt, 0 unreadable, `pg_amcheck` ok. Flawless: **True**. Details, guardrail denials and the files rAIden wrote itself: [results/SCOREBOARD.md](results/SCOREBOARD.md).

<!--/RESULTS-->

## How it works

```
 PostgreSQL 18 --jsonlog--> LogTailer --+
      ^                                 +--> classify --> Aggregator (debounce) --> Incident
      +-- pg_isready every 2 s ---------+    critical: panic / crash / corruption / DROP        |
                                                 a crash storm = ONE incident                  v
 patrol: checks.d/*.sh, deterministic, no LLM                                          agent session
   every 30 min AND on every config reload  --- only WARN/CRIT wakes the model --->    GPT-5.5 tool-calling loop
                                                                                       bash | read/write/edit_file | load_skill
                                                                                       pg_probe | escalate | finish
                                                             every call ---> Guard ---> audit.jsonl
                                                                                 |
   verify window (45 s, recurrence detection) <----------------------------------+
   evolve session (critical incidents only) --> new check + learned skill + lessons.md
                                                (validated: repaired or quarantined if broken)
```

* **Trigger**: a log tailer (rotation- and truncation-safe) plus a liveness probe. Events are classified by SQLSTATE and
  message into kinds with weights; ordinary application errors (constraint violations, syntax errors...) never wake the
  agent, and neither do the agent's own SQL errors. Storms are debounced into one incident (`RAIDEN_QUIET_S`,
  `RAIDEN_MAX_WAIT_S`).
  A configuration reload in the log (`received SIGHUP`, `cannot be changed without restarting the server`) is never an incident
  by itself, but it runs the patrol checks immediately: a restart-only change that sits on disk is a time bomb for the next restart.
* **Skills** (`.claude/skills/`): runbooks with exact commands and pitfalls, loaded on demand. `pg-triage` is the entry point
  and has a routing table. See [skills](.claude/skills/README.md).
* **Patrol** (`checks.d/*.sh`): deterministic health checks (liveness, wraparound, disk, archiver, backup freshness,
  sessions, integrity, crash recovery). They print `OK|WARN|CRIT name: message`; only WARN/CRIT wakes the agent. After an
  incident the checks that still fire are acknowledged for 30 minutes so a handled storm does not wake the agent twice.
* **Verify**: after `finish`, a 45 s window watches for the same critical kinds. A repeat marks the fix as failed and opens
  a recurrence incident.
* **Evolve**: after a *quiet* critical incident the agent gets a calm session to write a durable detector (`checks.d/`), a
  `learned-*` skill and lessons (`lessons.md` is injected into the next prompt). Every artifact is validated before use
  (frontmatter repaired when recoverable, otherwise quarantined; checks must run and print a verdict).

## The harness decision

"Which harness?" was a real question. Short version: a small purpose-built loop won, and the evolve loop is borrowed.

| Candidate | Verdict | Why |
|---|---|---|
| **Custom tool-calling loop** (this repo: loop + tools + LLM client ~500 lines, ~3k with the guardrails and the daemon; GPT-5.5 on Azure OpenAI, managed identity, no API keys) | **chosen** | a 24/7, event-triggered runner needs its own scheduler, debounce, budgets, audit and guardrails; all of that is the product |
| Claude Agent SDK on Foundry | feasible, **not used** | verified feasible (Foundry env vars, Entra auth, PreToolUse hooks, skills), but the sandbox subscription's Azure Marketplace policy refused every Claude deployment, so the model had to change; the loop is provider-agnostic by design (`raiden/llm.py` is ~50 lines) |
| ECC, nanobot, Hermes Agent | **ideas borrowed** | as far as I could tell they are interactive or chat-gateway harnesses, not log-triggered runners. Their learning loops (confidence-scored "instincts" that evolve into skills, "dream"-style memory consolidation, autonomous skill creation after hard tasks) shaped the evolve step |

## Guardrails (the referee)

The model is cooperative, but honest mistakes are how databases die. Every tool call passes `Guard.check()` first and is
written to `audit.jsonl` (allow or deny, with the rule). Defence in depth: OS privileges (an unprivileged user with a narrow
sudoers file), this policy engine, per-incident turn/time limits.

* judged on parsed tokens, so quoting tricks, multi-line scripts, loops, `$(...)`, `find -exec`, `xargs` do not hide a
  `pkill`: no mass kill, no `pg_resetwal`, no network or package installs, no power commands, no account or persistence
  changes, no reading credentials or the instance metadata endpoint
* data: no `DROP DATABASE/SCHEMA`, no `DROP TABLE`/`TRUNCATE`/`DELETE`/`UPDATE` without `WHERE` on application tables
  (scratch prefixes and PITR clones are fine), no mass `pg_terminate_backend`, never weaken durability (`fsync`,
  `full_page_writes`, `synchronous_commit`)
* paths: writes only in its own state, skills and checks, conf.d and `/tmp/raiden-*`; deletes only in its work area; the
  audit/event/restart files are append-only to it
* restarts: budgeted (6/hour). One narrow exception for a **hung** shutdown: `pg_ctl stop -m immediate` on production is
  allowed only when systemd reports the unit stuck `deactivating`/`activating` for 4 minutes, only with the exact data
  directory, once per hour. Signals to the postmaster stay forbidden.
* only SQL that *executes* is scanned (`psql -c/-f`, heredocs and pipes into `psql`), never prose in a note or a `jq` filter
* 120+ regression cases: `python -m raiden selftest` (also run by `raiden doctor` before the agent is armed)

The guard is a defence against honest mistakes, not a sandbox against an adversary: it is lexical, and the agent can run
commands as `postgres`. The OS-level privilege boundary is the primary control.

## The arena: nobody grades their own exam

`arena/` is the lab. The attacks live out of the agent's reach; the referee (`battle.py`) judges every round with the
**reconcile oracle** (`ledger.py`): a client-side ledger of every commit the application was *told succeeded*. A round is
won when every acknowledged commit is present, byte-identical and readable, the database is serving again, and rAIden is idle.
A commit whose connection died during `COMMIT` is in doubt and is neither demanded nor forbidden. The final verdict also
runs `pg_amcheck --heapallindexed`.

| Round | Attack | What it takes |
|---|---|---|
| crash | `pg_crash` loaded from a "temporary experiment" in `conf.d`: a random signal (9, 15, 3) to a random Postgres process every 4 s | root cause from `pg_settings`/`pg_file_settings`, one calm-window restart |
| toast | middle TOAST chunks of recent rows destroyed with `pg_surgery` | verify with amcheck, rebuild payloads from a PITR side clone |
| pitr | `DROP TABLE` of the ledger table | find the DROP's xid in the WAL, recover to just before it, restore owner and indexes |
| bomb | the same experiment file returns, the config is reloaded, and the restart that activates it is scheduled 5 minutes later (a maintenance window) | nothing is broken yet: only a defender that checks what the *next* restart will load can win before anything breaks (rAIden's own check from round 1) |
| rematch | the first attack again, immediate restart (optional round) | faster, with the learned skill |
| boss2 | `ALTER SYSTEM`: `postgresql.auto.conf` beats every `conf.d` file | the new move |

Four full runs were needed before one was clean. What each exposed (a real PostgreSQL 18.6 hang, a benign shutdown line counted
as a crash, a stale-state flapping bug in the liveness monitor, bugs in my own guard and attack script) is in
[results/RUN-HISTORY.md](results/RUN-HISTORY.md).

## Run the lab (Azure, nothing exposed, deleted afterwards)

```bash
deploy/azure/up.sh          # resource group, Foundry + gpt-5.5 deployment, an Ubuntu 24.04 VM with a managed identity
deploy/vm/run.sh -f deploy/vm/bootstrap.sh     # PostgreSQL 18 (PGDG), pg_crash built but not enabled, users, jsonlog, WAL archive
deploy/vm/push.sh           # ships this tree through Azure Run Command (no inbound ports) and installs it
deploy/vm/run.sh 'systemctl enable --now raiden'
deploy/vm/run.sh "systemd-run --unit raiden-battle /opt/raiden/venv/bin/python /var/lib/arena-private/battle.py --rounds crash,toast,pitr,bomb,boss2 --reset"
deploy/vm/peek.sh           # compact live status (Run Command returns at most ~4 KB)
deploy/vm/run.sh -f deploy/vm/bundle.sh && deploy/vm/fetch.sh /var/lib/arena/deck-bundle.tgz results/bundle.tgz
mkdir -p results/bundle && tar xzf results/bundle.tgz -C results/bundle --strip-components=1
python3 arena/report.py results/bundle   # -> results/SCOREBOARD.md and talk/data.json
deploy/azure/down.sh        # deletes the resource group, purges the soft-deleted AI account, verifies nothing is left
```

To run only the agent on your own Postgres 18 host: `pip install .`, set `RAIDEN_AOAI_ENDPOINT` /
`RAIDEN_AOAI_DEPLOYMENT` ([`deploy/vm/raiden.env.example`](deploy/vm/raiden.env.example)), `raiden doctor --llm`, then
`raiden run`. Other commands: `raiden replay <jsonlog>` (which incidents would wake the agent?), `raiden patrol`,
`raiden selftest`, `raiden once "text" --critical`.

## The deck

`talk/` holds the 10-slide lightning talk (Mortal Kombat announcer style, 1920x1080, vector art only) and a timed 5-minute speaker script.
Everything numeric in the deck and in `talk/script.md` is generated from the recorded battle (`arena/report.py` writes `talk/data.json`).

```bash
python3 -m venv .venv && .venv/bin/pip install -r talk/requirements.txt
.venv/bin/python arena/report.py results/bundle      # battle bundle -> talk/data.json, results/SCOREBOARD.md, this README block
.venv/bin/python talk/build.py --final               # talk/rAIden-pgconf-2026.pdf (headless Chrome) + talk/script.md
```

`build.py` refuses `--final` with sample or partial data. Headlines are real glyph outlines, so the PDF looks the same in Preview, Acrobat,
Chrome and poppler. `talk/make_qr.py` regenerates the QR code on the last slide.

## Honest limitations

* One run is one sample: an LLM is not deterministic. `results/` holds the recorded run and its raw numbers; the earlier runs
  and what they exposed are in `results/RUN-HISTORY.md`.
* The lab keeps WAL archive and base backups on the same VM. In production they must live elsewhere.
* The runbooks are generic, but the incident families (crash storm, TOAST, dropped table) were chosen for the talk and the
  skills were written with them in mind. The attacks themselves were not visible to the agent.
* The model is GPT-5.5 because Claude on Foundry was blocked in the sandbox subscription (see the table above).
* The time-bomb round is staged by the referee (config file + reload + a restart scheduled 5 minutes later). rAIden wins it only if a
  check it wrote earlier looks at what the *next restart* will load; in the recorded run it did, but that is one run of a
  non-deterministic model, and the fight is an attack on a lab database, not a benchmark.
* rAIden acts as a superuser on one database host. Treat it like any other privileged automation: start in a lab.

## Credits

[`pg_crash`](https://github.com/cybertec-postgresql/pg_crash) by CYBERTEC (the chaos monkey). Ideas for the evolve loop from
Hermes Agent, ECC and nanobot. Fonts (SIL OFL): Anton, Rubik Wet Paint, Press Start 2P, Teko, Barlow Condensed, JetBrains
Mono. *Mortal Kombat* is a trademark of its owners; this talk is an affectionate parody with original artwork and is not
affiliated with or endorsed by them. MIT licensed.
