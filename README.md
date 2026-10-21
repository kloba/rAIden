<div align="center">

# rAIden

**The AI on-call DBA for your PostgreSQL fleet.**

rAIden watches every PostgreSQL server you run, on-prem or in any cloud. When something goes wrong it opens an incident,
investigates it like a senior DBA, fixes it behind guardrails and proves the fix, and it asks a person exactly where you
want a person to decide.

[Quick start](#quick-start) · [Tour](#a-tour) · [Any PostgreSQL](#any-postgresql-anywhere) · [How it works](#how-it-works) ·
[Deploy](#deploy-it) · [Security](#security) · [Tested on real servers](#tested-on-real-servers) · [Production readiness](docs/production-readiness.md)

</div>

![The estate overview: the fleet at a glance, the last shift on one timeline and the decisions waiting for a person](docs/screenshots/overview.png)

## What it does

* **Monitors any PostgreSQL 13 or newer** through a read-only `pg_monitor` login: 96 metrics, among them all 74 that
  [Azure Monitor defines for PostgreSQL](https://learn.microsoft.com/en-us/azure/postgresql/monitor/concepts-monitoring)
  under the same names, from SQL, from Azure Monitor and from the rAIden host agent; 23 built-in monitors; live views of
  sessions and lock queues, top queries, tables and vacuum, replication and configuration.
* **Runs every incident through one circuit**: Detected → Triage → Diagnose → Remediate → Verify → Harden → Resolved.
  rAIden does the work of every stage. A breaker in front of each stage decides whether it carries on by itself or stops
  for a person, per environment, and any server can pin its own.
* **Changes things only through typed actions**: 15 of them, from cancelling a query to dropping a replication slot, each
  with a risk level. High-risk actions always wait for a person, and the approved plan is the contract: anything outside
  it is a new decision.
* **Onboards a database the way a senior DBA would**: a connection test that checks the grants, the preparation SQL for
  each platform, an assessment in six areas with a score, sizing from the host profile, and one-click fixes that go
  through the same approvals.
* **Does the routine work on a schedule**: checks that cost no tokens (health patrol, configuration drift, wraparound,
  security posture, backup readiness) and AI reviews (daily health report, index advisor, vacuum and bloat), with cron
  and time zones.
* **Answers questions in chat**: ask "who is blocking whom?" and rAIden reads the server with read-only tools, shows
  every step and proposes a change inline for you to approve.
* **Learns from your team**: what people teach it, the reasons they give for their decisions and the incidents it resolves
  become instructions and playbooks for every prompt they apply to, so nobody has to say it twice. A plan people approve
  every time stops being a question once an admin accepts, and the next rejection takes that back. Once a night it
  dreams over what is new, and every dream can be undone.
* **Uses the models you choose**: Azure OpenAI (Entra ID or key), OpenAI, Anthropic, or any OpenAI-compatible server
  (vLLM, Ollama, LM Studio, a gateway). Routing per purpose with fallbacks, daily token and monthly cost budgets, and
  token accounting per incident, server and purpose.
* **Keeps a record**: every action, decision and change in an append-only audit log; admin, operator and viewer roles;
  notifications to Microsoft Teams, Slack or any webhook; a Prometheus endpoint.

## A tour

### The incident board

![The incident board: one column per stage, a breaker above each one](docs/screenshots/incidents.png)

Each column is a stage. The breaker above it shows whether rAIden carries on by itself (autopilot) or stops until a person
approves; switch between the production, staging and development policies at the top right. Cards that wait for you
say what for.

### An incident

![An incident waiting at the production gate with its root cause, plan and signal](docs/screenshots/incident.png)

A deploy started filtering a large table on a column without an index. rAIden found the query, wrote the root cause and
a one-step plan with its risk, and stopped at the production gate. Under the plan it says how often people approved the
same fix here: after three approvals in a row it offers not to ask again. The signal chart shows the metric that fired
and the rule; the timeline below has every tool call with its result, every decision and who made it.

### What rAIden learned

![The Knowledge page: a proposal not to ask again, an inferred rule waiting for a person, the instructions and the dream journal](docs/screenshots/knowledge.png)

Alex approved the same plan for a lock queue in production three times, so rAIden offers not to ask again; an inferred
rule waits for a person to confirm it. Below are the rules, facts and preferences people taught it, each with its scope
and how often it went into a prompt, and on the right the journal of its nightly dreams. See
[how rAIden learns](docs/learning.md).

### Servers

<table>
<tr>
<td width="50%"><img src="docs/screenshots/estate.png" alt="Estate"><br><b>Estate.</b> Every server with its status, issues and suggestions, sparklines and the gates where a person decides.</td>
<td width="50%"><img src="docs/screenshots/server.png" alt="Server overview"><br><b>A server.</b> Key metrics and charts, what needs attention, and tabs for metrics, sessions, queries, tables, replication, configuration and assessments.</td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/sessions.png" alt="Sessions and locks"><br><b>Sessions and locks.</b> The head of the lock queue, everything waiting behind it, and the session to end.</td>
<td width="50%"><img src="docs/screenshots/performance.png" alt="Performance"><br><b>Performance.</b> Any metric across the fleet; each server keeps its colour everywhere.</td>
</tr>
</table>

### Investigate

![Investigate: a question, the tool rAIden used, its answer and an action waiting for approval](docs/screenshots/chat.png)

Ask about one server or the whole estate. rAIden answers from live data with read-only tools; when you ask it to fix
something, the change appears inline and runs only after you approve it.

### Onboarding a database

<table>
<tr>
<td width="50%"><img src="docs/screenshots/onboard-connect.png" alt="Connect"><br><b>Connect.</b> The least-privilege login for the platform you picked, and a connection test that checks every grant.</td>
<td width="50%"><img src="docs/screenshots/onboard-assess.png" alt="Assess and tune"><br><b>Assess and tune.</b> A score in six areas, what to do first, sizing for the host, and fixes you can apply on the spot.</td>
</tr>
</table>

### Automation and settings

<table>
<tr>
<td width="50%"><img src="docs/screenshots/routines.png" alt="Routines"><br><b>Routines.</b> Scheduled checks and AI reviews, with the history of every run.</td>
<td width="50%"><img src="docs/screenshots/monitors.png" alt="Monitors"><br><b>Monitors.</b> Alert rules on any metric that open an incident, notify, or leave a suggestion.</td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/autonomy.png" alt="Autonomy"><br><b>Autonomy.</b> The breakers for every environment, the guardrails that always apply, and the servers that pin their own gates.</td>
<td width="50%"><img src="docs/screenshots/ai-endpoints.png" alt="AI endpoints"><br><b>AI endpoints.</b> Which model does what, what it costs, and the budgets that stop it.</td>
</tr>
<tr>
<td width="50%"><img src="docs/screenshots/databases.png" alt="Databases"><br><b>Databases.</b> Connections, data sources, grants and the latest assessment of every server.</td>
<td width="50%"><img src="docs/screenshots/approvals.png" alt="Needs you"><br><b>Needs you.</b> Every decision waiting for a person, with the context to make it.</td>
</tr>
</table>

### Night shift

<table>
<tr>
<td width="50%"><img src="docs/screenshots/overview-dark.png" alt="The estate overview in the dark theme"></td>
<td width="50%"><img src="docs/screenshots/incidents-dark.png" alt="The incident board in the dark theme"></td>
</tr>
</table>

## Any PostgreSQL, anywhere

rAIden speaks SQL to every server, so anything that runs PostgreSQL 13 or newer works. Where the platform has more to
offer, it uses that too.

| Where it runs | Connection | CPU, memory, disk | Configuration changes |
|---|---|---|---|
| On-prem, bare metal | SQL | rAIden host agent | `ALTER SYSTEM`, through the approvals |
| Virtual machine (any cloud or hypervisor) | SQL | rAIden host agent | `ALTER SYSTEM`, through the approvals |
| Kubernetes (CloudNativePG, Zalando, Crunchy, StatefulSets) | SQL through the read-write service | your cluster monitoring | the change to make in the manifest |
| Azure Database for PostgreSQL flexible server | SQL | Azure Monitor | the change to make in Server parameters |
| Amazon RDS, Amazon Aurora | SQL | not collected | the change to make in the parameter group |
| Google Cloud SQL | SQL | not collected | the change to make in the database flags |
| Anything else that speaks the protocol | SQL | the host agent, if you can install it | depends on the platform |

Session actions (cancel, terminate, role limits and timeouts), index builds and vacuum work everywhere the login has the
grants for them; the onboarding page shows the grants for each platform. On managed services, settings that only the
provider can change come with the provider's steps instead of a button.

On Azure Database for PostgreSQL, the assessment also reads the server's Azure resource: whether Microsoft Entra
authentication is on and enforced, whether the data is encrypted with a customer-managed key, and whether the server is
open to public networks, each with the steps in the Azure portal. These are the checks of Microsoft Fabric's
[Database Hub monitoring for PostgreSQL](https://learn.microsoft.com/en-us/fabric/database/hub/monitor-postgresql).

![The security area of an Azure server's assessment: Entra authentication, customer-managed key and public network access, read from the Azure resource, with the portal steps](docs/screenshots/azure-security.png)

## Quick start

### Try the demo

Nothing real is contacted: seven simulated servers on six platforms with 30 days of history, and a scripted model.

```bash
docker build -t raiden-console .
docker run --rm -p 8080:8080 -e RAIDEN_DEMO=1 raiden-console
```

Open http://localhost:8080 and sign in with the demo account shown on the sign-in page. Within a few minutes a lock queue
builds up on orders-prod, a connection storm hits analytics-staging, a new query saturates payments-prod's CPU and a
replica falls behind. Approve the gates and watch rAIden fix them in the simulation. To try onboarding, open
**Databases → Add a database** and use the demo server.

Without Docker (Python 3.10+, Node 22 for the UI):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[console]"
(cd web && npm ci && npm run build)
raiden console --demo --port 8787          # http://127.0.0.1:8787
```

### Run it on your fleet

1. Start the console with a PostgreSQL state store:
   ```bash
   cp deploy/console/console.env.example .env   # set RAIDEN_SECRET_KEY and RAIDEN_STATE_PASSWORD
   docker compose up -d
   ```
2. Open it. The first visit creates the admin account (or run
   `docker compose exec console raiden console-user you@example.com --role admin`).
3. **AI endpoints → Add endpoint**: the model rAIden thinks with and what it is used for.
4. **Databases → Add a database**: pick the platform, create the monitoring login with the SQL shown, test the
   connection, describe the host and review the assessment.
5. **Autonomy**: decide where a person approves. Production starts with approvals before Remediate and Harden.

## How it works

```
 Browser ──HTTPS──▶ rAIden Console          UI · REST API · live events (SSE) · state in SQLite or PostgreSQL
                    │
                    │ Collectors ──▶ Metric store ──▶ Monitors ──▶ Incident pipeline ──▶ AI router ──▶ your models
                    │  SQL            raw samples      rules       stages and gates      routing        Azure OpenAI
                    │  Azure Monitor  + 15 min                     typed actions         fallbacks      OpenAI, Anthropic
                    │  host agent     rollups                      SQL guard, audit      budgets        OpenAI-compatible
                    │
                    │ Routines (cron) · Investigate (chat) · Knowledge and dreams · Assessments · Notifications · Audit log
                    │
                    ├── read-only login (pg_monitor) and typed actions ──▶ PostgreSQL on-prem, VMs, Kubernetes, Azure, AWS, GCP
                    └── heartbeats, host metrics, log-driven incidents ◀── rAIden host agent (optional, on the database host)
```

One process does it all: the API and the UI, collection on every server's own interval, monitor evaluation after each
collection, the incident pipeline, routines, chat and notifications. Incidents survive restarts: an interrupted stage is
picked up where it stopped.

### The incident circuit

| Stage | What rAIden does | Gate in front of it: production · staging · development |
|---|---|---|
| Detected | A monitor, a routine, a chat, the host agent or a person opens the incident | |
| Triage | Reads the metrics and the recent history, sets the severity and a first hypothesis | autopilot · autopilot · autopilot |
| Diagnose | Investigates with read-only tools until it has the root cause and a plan | autopilot · autopilot · autopilot |
| Remediate | Carries out the approved plan, one typed action at a time | **approval** · autopilot · autopilot |
| Verify | Watches the metrics for the verification window; if the problem comes back, the incident reopens | autopilot · autopilot · autopilot |
| Harden | Proposes protections: monitors, role limits and timeouts, routines, lessons for next time; what its login may not do goes to the server's suggestions for a person | **approval** · **approval** · autopilot |
| Resolved | Writes the report | autopilot · autopilot · autopilot |

Rules that hold whatever the breakers say:

* the approved plan is the contract: in Remediate, an action that is not in the plan is a new decision;
* rAIden plans only what its login may do on that server (it knows the grants from the connection test) and names the
  rest for a person;
* high-risk actions (drop a replication slot, roll back a prepared transaction, free SQL) always wait for a person;
* a plan longer than the limit you set (8 actions by default) always waits for a person;
* with no AI endpoint or budget left, every gate turns into an approval and incidents wait for a person instead of
  failing silently. Monitors and check routines keep running.

One gate opens without a person when people have already said yes often enough: a plan approved 3 times in a row for the
same kind of incident, once an admin accepts not to be asked again. Never for a high-risk action, and the next rejection
takes it back ([how rAIden learns](docs/learning.md)).

### The host agent

`raiden run` is an optional daemon for the database host. It reads what SQL cannot see, the PostgreSQL log and the
operating system, and handles crashes, corruption, a dropped table or a configuration time bomb on its own, behind its
own guardrails. Linked to the console with an agent token, it sends host metrics every 30 seconds and puts the incidents
it handles on the board. See [docs/host-agent.md](docs/host-agent.md).

It has been tested under chaos: TOAST corruption, a dropped table, a connection flood, a lock freeze and the pg_crash
extension on PostgreSQL 18, each attack repeated after the agent had hardened the server. Every attack was recovered and
every repeat was blocked, with 125,269 acknowledged commits and none lost, counted by an independent oracle of commits
rather than by the model ([the chaos tests](docs/chaos-tests.md)).

## Deploy it

| | |
|---|---|
| **Container** | `docker build -t raiden-console .` The image installs the exact, hash-checked dependencies of [`requirements/console.lock`](requirements/console.lock), runs as an unprivileged user (uid 10001), keeps its state in `/data`, listens on 8080 and has a health check (`/readyz`; `/healthz` for liveness). |
| **Docker Compose** | [`docker-compose.yml`](docker-compose.yml): the console and a PostgreSQL 17 state store. |
| **systemd** | Build the UI into the package (`make web`), install it into a virtual environment (`pip install ".[console]"`) and use the hardened unit in [`deploy/console/raiden-console.service`](deploy/console/raiden-console.service) with [`console.env.example`](deploy/console/console.env.example). |
| **TLS** | Put it behind your reverse proxy and set `RAIDEN_BASE_URL=https://...`: notification links use it and cookies become Secure. Live updates use Server-Sent Events, so turn off response buffering for `/api/v1/events`. |
| **Backups** | Back up the state store (the SQLite file in the data directory, or the PostgreSQL database) and keep `RAIDEN_SECRET_KEY` with it: without the key, the stored database passwords and API keys cannot be decrypted. |

Run one console per state store: it schedules collection, incidents and routines itself. A second console on the same
store refuses to start and says which one holds it (a lock file next to SQLite, an advisory lock on PostgreSQL).

Before you put it in front of production servers, read [docs/production-readiness.md](docs/production-readiness.md):
what has been measured, what is enforced, and what is still missing.

### Configuration

Everything about the fleet (servers, AI endpoints, autonomy, monitors, routines, notifications, retention, people) is
configured in the console and kept in the state store. The process itself takes a few environment variables:

| Variable | Default | |
|---|---|---|
| `RAIDEN_SECRET_KEY` | generated once into `<data>/secret.key` | Signs sessions and encrypts stored passwords and API keys. Set it in production. |
| `RAIDEN_CONSOLE_DATA` | `~/.local/share/raiden-console` (`/data` in the image) | The data directory. |
| `RAIDEN_CONSOLE_DB` | SQLite in the data directory | `postgresql://user:password@host:5432/raiden` for a PostgreSQL state store. |
| `RAIDEN_CONSOLE_HOST`, `RAIDEN_CONSOLE_PORT` | `127.0.0.1`, `8080` (`0.0.0.0` in the image) | Where it listens. |
| `RAIDEN_BASE_URL` | | The public URL, for notification links; `https://` also marks cookies Secure. |
| `RAIDEN_SESSION_HOURS` | `12` | How long a sign-in lasts. |
| `RAIDEN_MAX_AI_SESSIONS` | `3` | AI sessions that may run at once (incidents and routines; chat is not counted). |
| `RAIDEN_METRICS_TOKEN` | | Turns on `/api/v1/prometheus` for scrapers that send this bearer token. |
| `RAIDEN_DEMO`, `RAIDEN_DEMO_SPEED` | off, `1` | The simulated fleet; a higher speed makes its scenarios come sooner. |
| `RAIDEN_LOG_LEVEL`, `RAIDEN_LOG_FORMAT` | `info`, `text` | `json` writes one JSON object per line for your log pipeline. |
| `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_CLIENT_SECRET` | | Only without a managed identity: Azure Monitor metrics, the Azure security assessments (both need *Monitoring Reader*) and Entra ID for Azure OpenAI use `DefaultAzureCredential`. |

### API

Everything the UI does goes through the REST API under `/api/v1`; the OpenAPI reference is served at `/api/docs`.
`/api/v1/prometheus` exports the latest value of every collected metric per server (`raiden_pg_*`), open incidents per
server and pending approvals, and the console's own health: whether each server answers (`raiden_server_up`), how old
its latest numbers are, and the day's tokens and failed model calls, so you can alert on the console itself.

## Security

* **Least privilege.** The monitoring login needs `pg_monitor` and nothing else; each kind of action is a grant you choose
  to give (`pg_signal_backend`, `pg_maintain`, `CREATEROLE`). rAIden warns when it is connected as a superuser.
* **No free-form writes.** Questions run in a `READ ONLY` transaction with statement and lock timeouts, and functions that
  act even there (`pg_terminate_backend`, `pg_reload_conf`, `dblink`...) are refused. Changes are typed actions with a
  risk level; free SQL is a high-risk action that always waits for a person and is checked against the host agent's
  guardrails.
* **Secrets.** Database passwords and API keys are encrypted at rest with a key derived from `RAIDEN_SECRET_KEY`; people's
  passwords are hashed with scrypt; host agent tokens are stored as hashes and shown once.
* **Sessions.** Signed, HttpOnly, SameSite cookies (Secure over HTTPS); every change needs a header a cross-site form
  cannot send. Roles: viewers read, operators decide and act, admins configure. Failed sign-ins are throttled per
  account and address, and pages carry a Content-Security-Policy that allows scripts only from the console.
* **Accountability.** Every action, decision, sign-in and configuration change is in the audit log with who, what, on
  which server and whether it was allowed. An action a person runs from the console is recorded under their name.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Limits worth knowing

* The console's SQL collector and actions were validated on self-managed PostgreSQL 13 to 18 and a standby
  ([docs/validation.md](docs/validation.md)), not yet on managed services. Point it at a staging server before you trust it
  with production, and keep the production gates on approval until you have seen how it works on your workload.
* rAIden's reasoning is only as good as the model behind it. That is why it acts through typed actions, the autonomy
  policy and the guardrails, never through free text.
* CPU, memory and disk on Amazon RDS, Aurora and Cloud SQL are not collected yet (no CloudWatch or Cloud Monitoring
  integration); everything PostgreSQL reports is.
* On Kubernetes and managed services, rAIden tells you which setting to change where; it does not change the manifest
  or the provider's configuration itself.
* One console per state store, and no failover between consoles yet: if the console is down, nothing is collected and
  no incident moves until it is back.
* This is the console's first release: the state store is created on the first start and there are no schema migrations
  yet. Back it up before you upgrade.

## Development

```bash
make install     # the Python package (editable, with the console and test extras) and the UI's packages
make demo        # build the UI and run the console on the demo fleet: http://127.0.0.1:8787
make dev         # a hot-reloading UI on :5173 against `make demo`
make test        # unit and API tests: no PostgreSQL and no model needed
make test-all    # plus the two-minute end-to-end incident on the demo fleet
make lock        # re-pin the console's dependencies (requirements/console.lock) after changing pyproject.toml
make docker      # the container image
```

The checks against real servers and a real model run on a lab VM, never on a laptop: `deploy/vm/console-lab.sh` sets up
PostgreSQL 13 to 18 with a standby, and `tests/live/` holds the checks ([docs/validation.md](docs/validation.md)).
`python -m tests.bench_metric_store` measures the metric store on your hardware, and `make a11y` checks every page of a
running `make demo` against WCAG 2.1 AA.

| Path | |
|---|---|
| `raiden/console/` | The console: API (FastAPI), incident pipeline, collectors and drivers, monitors, routines, chat, AI router, assessment, demo fleet |
| `web/` | The UI: React, TypeScript, Vite, Tailwind CSS, TanStack Query, Recharts |
| `raiden/` | The host agent (`raiden run`): log tailer, classifier, guardrails, agent loop, patrol, evolve, console link |
| `checks.d/`, `.claude/skills/` | The host agent's deterministic checks and runbooks |
| `deploy/console/` | The console's systemd unit and environment file |
| `tests/` | pytest: guardrails, agent loop and daemon, console API, driver and units, the end-to-end incident; `tests/live/`: the checks against real servers; `tests/chaos/`: the host agent's chaos tests (attacks, referee, oracle) and their recorded results |
| `deploy/azure/`, `deploy/vm/` | The Azure lab (a VM and a model, nothing exposed): the [validation on real servers](docs/validation.md) and the [chaos tests](docs/chaos-tests.md) |
| `docs/` | [Production readiness](docs/production-readiness.md), [validation on real servers](docs/validation.md), [the host agent](docs/host-agent.md), [its chaos tests](docs/chaos-tests.md), screenshots |
| `CHANGELOG.md`, `SECURITY.md` | [What changed in each release](CHANGELOG.md), [how to report a vulnerability](SECURITY.md) |

## Tested on real servers

Before this release the console ran against real servers on an Azure VM: PostgreSQL 13 to 18 and a streaming standby,
the least-privilege login from the onboarding page, a real lock queue, and `gpt-6.1-sol` deciding. 240 driver checks and
32 end-to-end checks pass, and what the runs found is fixed: [docs/validation.md](docs/validation.md).

<table>
<tr>
<td width="50%"><img src="docs/screenshots/live-estate.webp" alt="The lab's estate: PostgreSQL 13 to 18 and a standby"><br><b>The lab's estate.</b> Six PostgreSQL versions and a standby, onboarded through the API; the chips are what the assessment found on each version's defaults.</td>
<td width="50%"><img src="docs/screenshots/live-incident.webp" alt="The real lock queue on pg17, resolved"><br><b>A real incident.</b> A forgotten transaction, a migration waiting behind it and every writer queued: diagnosed by the model, fixed after one approval, resolved in 2 min 40 s.</td>
</tr>
</table>

## License

MIT, see [LICENSE](LICENSE).
