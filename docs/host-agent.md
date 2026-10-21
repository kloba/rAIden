# The rAIden host agent

`raiden run` is the part of rAIden that lives **on the database host**. The console sees what PostgreSQL reports over SQL;
the host agent sees what only the server can see: the PostgreSQL log, the operating system and the files. It wakes up on
crashes, corruption, a dropped table or a configuration time bomb, finds the root cause, fixes it behind hard guardrails,
proves the fix with an independent check, and then writes itself a new detector and a runbook for next time.

It came first, and the console grew around it. It runs on its own, or linked to a console. The
[chaos tests](chaos-tests.md) show it against a corrupted table, a dropped table, a connection flood, a lock freeze and a
crash storm.

## Linking it to the console

On the server's **Settings** tab in the console, create an agent token. The console shows three lines for the agent's
environment file (`/etc/raiden/raiden.env`):

```bash
RAIDEN_CONSOLE_URL=https://raiden.example.com
RAIDEN_CONSOLE_TOKEN=...          # shown once; the console stores only its hash
RAIDEN_SERVER_ID=payments-prod
```

With them set, the daemon

* sends a heartbeat every 30 seconds with host metrics read from `/proc` (CPU, memory, load, disk I/O and queue depth,
  network, storage of the data directory), so an on-prem server or a VM gets the same charts as a managed service, and
* forwards the incidents it handles on its own (opened, every tool call, guardrail denials, escalations, closed), so they
  appear on the console's incident board next to the ones the console found.

Nothing is sent when the variables are not set. A console that is down never slows the daemon: events wait in a bounded
queue (500) and are retried with backoff. The console treats a repeated event as the same incident.

## How it works

```
 PostgreSQL 18 --jsonlog--> LogTailer --+
      ^                                 +--> classify --> Aggregator (debounce) --> Incident
      +-- pg_isready every 2 s ---------+    critical: panic / crash / corruption / DROP        |
                                                 a crash storm = ONE incident                  v
 patrol: checks.d/*.sh, deterministic, no LLM                                          agent session
   every 30 min AND on every config reload  --- only WARN/CRIT wakes the model --->    LLM tool-calling loop
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
  A wait for a *table* lock (`log_lock_waits`, "still waiting for ...Lock on relation") is a critical kind: a DDL statement that
  waits for an old transaction freezes every reader and writer queued behind it. A role that hits its own connection limit is
  not an incident (that is a protection working); a full server (`too many clients`) is.
* **Skills** (`.claude/skills/`): runbooks with exact commands and pitfalls, loaded on demand. `pg-triage` is the entry point
  and has a routing table. See [skills](../.claude/skills/README.md). `postgres-best-practices` vendors five references of Microsoft's
  [postgres-skills](https://github.com/microsoft/postgres-skills) (MIT, verbatim, with the router rewritten for an autonomous
  agent on a self-managed server: the rest of that repo targets Azure managed servers and application design).
* **Patrol** (`checks.d/*.sh`): deterministic health checks (liveness, wraparound, disk, archiver, backup freshness,
  sessions, integrity, crash recovery). They print `OK|WARN|CRIT name: message`; only WARN/CRIT wakes the agent. After an
  incident the checks that still fire are acknowledged for 30 minutes so a handled storm does not wake the agent twice.
* **Verify**: after `finish`, a 45 s window watches for the same critical kinds. A repeat marks the fix as failed and opens
  a recurrence incident.
* **Evolve**: after a *quiet* critical incident the agent gets a calm session to write a durable detector (`checks.d/`), a
  `learned-*` skill and lessons (`lessons.md` is injected into the next prompt). Every artifact is validated before use
  (frontmatter repaired when recoverable, otherwise quarantined; checks must run and print a verdict).

## Guardrails

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
commands as `postgres`. The OS-level privilege boundary is the primary control. Treat the host agent like any other
privileged automation: start in a lab.

## Install it on a host

```bash
python3 -m venv /opt/raiden/venv && /opt/raiden/venv/bin/pip install .     # from this repository
cp deploy/vm/raiden.env.example /etc/raiden/raiden.env   # the model endpoint, and the console lines above
/opt/raiden/venv/bin/raiden doctor --llm                 # checks the installation, the guardrails and the model
/opt/raiden/venv/bin/raiden run                          # the daemon
```

[`deploy/vm/bootstrap.sh`](../deploy/vm/bootstrap.sh) and [`deploy/vm/install.sh`](../deploy/vm/install.sh) are the
complete setup used in the lab: the agent running as an unprivileged `raiden` user with a narrow sudoers file, the code
root-owned (the agent cannot edit its own guardrails), skills and checks writable by it, and a systemd unit
([`deploy/vm/raiden.service`](../deploy/vm/raiden.service)). They also install PostgreSQL 18 and the arena, which a
production host does not need.

Other commands: `raiden replay <jsonlog>` (which incidents would a recorded log wake the agent for?), `raiden patrol`,
`raiden selftest`, `raiden once "text" --critical`, `raiden fortify` (day-0 hardening: verified backup, streaming replica,
baseline configuration, new checks).

The agent speaks to Azure OpenAI with the host's managed identity (no API keys): chat completions for GPT-5.x and the
Responses API for the GPT-6 family. Its runbooks assume PostgreSQL on Linux with systemd (tested on Ubuntu 24.04 with the
PGDG packages, PostgreSQL 18).

## The harness decision

"Which harness?" was a real question. Short version: a small purpose-built loop won, and the evolve loop is borrowed.

| Candidate | Verdict | Why |
|---|---|---|
| **Custom tool-calling loop** (this repo: loop + tools + LLM client ~500 lines, ~3k with the guardrails and the daemon; a model on Azure OpenAI, managed identity, no API keys) | **chosen** | a 24/7, event-triggered runner needs its own scheduler, debounce, budgets, audit and guardrails; all of that is the product |
| Claude Agent SDK on Foundry | feasible, **not used** | verified feasible (Foundry env vars, Entra auth, PreToolUse hooks, skills), but the sandbox subscription's Azure Marketplace policy refused every Claude deployment, so the model had to change; the loop is provider-agnostic by design (`raiden/llm.py` speaks chat completions for GPT-5.x and the Responses API for the GPT-6 family, which refuses function tools with reasoning on chat completions) |
| ECC, nanobot, Hermes Agent | **ideas borrowed** | as far as I could tell they are interactive or chat-gateway harnesses, not log-triggered runners. Their learning loops (confidence-scored "instincts" that evolve into skills, "dream"-style memory consolidation, autonomous skill creation after hard tasks) shaped the evolve step |
