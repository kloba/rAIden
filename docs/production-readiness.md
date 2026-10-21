# Production readiness

rAIden Console 0.2.0, assessed on 22 October 2026. For every area: where it stands, the evidence, and what is still
missing. *Ready* means we would run it on production servers today with the defaults; *ready, with limits* means ready
within the limits named; *not yet* means do not rely on it.

## At a glance

| Area | Status | Evidence | Still missing |
|---|---|---|---|
| [Works on real PostgreSQL](#works-on-real-postgresql) | Ready on self-managed PostgreSQL 13 to 18 | 240 driver checks on six versions and a standby; 32 end-to-end checks with a real model, three runs | managed services, Kubernetes and TLS to the servers not run live |
| [The changes it makes](#the-changes-it-makes) | Ready | typed actions, approvals, the plan as the contract, plans checked against the login's grants, timeouts, learned approvals only after an admin accepts | |
| [Sign-in and access](#sign-in-and-access) | Ready, with limits | scrypt, signed cookies, CSRF header, three roles, throttled sign-in, security headers, audit log | single sign-on, MFA, per-environment roles, API tokens for people |
| [Secrets](#secrets) | Ready, with limits | database passwords and API keys encrypted at rest; agent tokens hashed; Entra ID without keys | key rotation |
| [Staying up](#staying-up) | Ready, with limits | incidents resume after a restart; one console per state store, enforced; notifications retried | failover between consoles; schema migrations |
| [Scale](#scale) | Measured on one machine | a collection round of 300 servers in 47 ms; 59 MB of store a server at the default retention | a fleet of hundreds of real servers |
| [Watching the console](#watching-the-console) | Ready | health and readiness endpoints, Prometheus with the console's own health, JSON logs | tracing |
| [Accessibility](#accessibility) | Ready | no WCAG 2.1 A or AA violation on 19 pages in both themes (axe-core) | a screen reader walkthrough |
| [Supply chain](#supply-chain) | Ready, with limits | locked npm and hash-pinned Python dependencies, `pip check`, an unprivileged image | SBOM, signed images, base images pinned by digest |
| [Tests and CI](#tests-and-ci) | Ready | 196 tests, the end-to-end incident, lint, an image smoke test on every push | browser and accessibility checks in CI |
| [AI: cost, control, data](#ai-cost-control-and-data) | Ready, with limits | budgets, routing per purpose, token accounting; 82,167 tokens for the real incident | redaction of literals in statement text |

[Before you point it at production](#before-you-point-it-at-production) is the checklist; [what comes next](#what-comes-next)
is the list of gaps in the order production users will meet them.

## Works on real PostgreSQL

The console and its driver ran against PostgreSQL 13, 14, 15, 16, 17 and 18 and a streaming standby on an Azure VM, with
the least-privilege login the onboarding page asks for and `gpt-6.1-sol` deciding: 240 driver checks (every read tool,
the SQL guard, a real lock queue, all 15 typed actions with rAIden's login and with a superuser) and 32 end-to-end checks
through the API, from the first sign-in to a resolved incident. Everything those runs found is fixed and has a test
where a test needs no server. [docs/validation.md](validation.md) has the lab, the results and the findings.

Not run against a real server yet:

* **Managed services.** Azure Database for PostgreSQL, Amazon RDS and Aurora, Google Cloud SQL. The driver uses only
  standard catalog views, but each provider's restrictions are untested, and so are Azure Monitor's platform metrics and
  the four Azure security assessments (unit-tested against the documented shape of the resource).
* **Kubernetes** operators, and **TLS** to the database servers (the lab ran on localhost).
* **Time.** The lab ran for two hours, not weeks: retention and roll-ups are covered by unit tests only.

Start with a staging server, and keep the production gates on approval until you have watched a few incidents.

## The changes it makes

* rAIden changes a server only through 15 typed actions, each with a risk level. High-risk ones (drop a replication
  slot, roll back a prepared transaction, free SQL) always wait for a person, and free SQL is also checked against the
  host agent's guardrails.
* The approved plan is the contract: in Remediate, an action that is not in it is a new decision.
* Plans and protections are checked against the grants of rAIden's login on that server (read in the connection test),
  and against the platform (no `ALTER SYSTEM` on a managed service). What it may not do goes to the server's suggestions
  with the SQL, for a person.
* Every statement runs with statement and lock timeouts (3 seconds of lock wait; 5 minutes for concurrent index builds,
  which also drop their unfinished index when they fail). rAIden opens at most three short connections per server next to
  the collector's, inside the `CONNECTION LIMIT 5` of the onboarding SQL.
* With no AI endpoint or no budget left, every gate turns into an approval: incidents wait for a person instead of
  failing silently.
* A remediation plan goes ahead without a person only where people already said yes: the same action for the same kind
  of incident approved 3 times in a row, and an admin accepted not to be asked again. Never for high-risk actions; the
  next rejection takes it back; the decision is on the record as *learned approval #n*
  ([how rAIden learns](learning.md)). What rAIden learns otherwise (instructions, playbooks) shapes its reasoning and
  lifts no gate.

Evidence: the real incident in [the validation](validation.md#the-incident) (cancel of the waiting `ALTER TABLE` after a
person approved it, 2 min 40 s from detection to the report) and the end-to-end test on the demo fleet.

## Sign-in and access

What is in place:

* Passwords are hashed with scrypt (at least 10 characters). Sessions are signed, HttpOnly, SameSite cookies, Secure
  over HTTPS, for 12 hours; disabling a person or resetting their password signs them out everywhere.
* Every change needs a header a cross-site form cannot send. Roles: viewers read, operators decide and act, admins
  configure.
* Sign-in is throttled: 5 failures per account and address, or 20 per address, in 15 minutes, then `429` with
  `Retry-After`. A sign-in for an account that does not exist takes as long as a wrong password, so the answers do not
  reveal which accounts exist.
* Pages carry a Content-Security-Policy that allows scripts only from the console (and the one inline script that sets
  the theme, by its hash), no framing, and the usual headers (`nosniff`, `Referrer-Policy`, `no-store` on the API,
  HSTS over HTTPS).
* Every sign-in, allowed or denied, every change, decision and action is in the audit log with who, what and where.

Missing:

* **Single sign-on and MFA.** Until OIDC is in, put the console behind your identity-aware proxy as well.
* **Per-environment roles.** An operator can approve on every server.
* **API tokens for people.** Scripts sign in like a person; host agents have their own tokens.

## Secrets

`RAIDEN_SECRET_KEY` signs sessions and encrypts the stored database passwords and API keys; without it, a key is
generated once into the data directory (mode 0600). Host agent tokens are stored as hashes and shown once. Azure OpenAI
and Azure Monitor can use a managed identity or Entra ID, so no key is stored at all.

Missing: **key rotation.** A new key makes the stored passwords and API keys unreadable, so they have to be entered
again. Inject the key from your secret store (Key Vault, Vault, Kubernetes secrets) as an environment variable.

## Staying up

* The state lives in SQLite (WAL) or PostgreSQL. After a restart, interrupted incidents go back to the queue and pick
  up where they were: a session waiting at a gate keeps its whole conversation in the store.
* **One console per state store, enforced.** A second console refuses to start and names the one that holds the store
  (a lock file next to SQLite, a session advisory lock on PostgreSQL). If the PostgreSQL store restarts, the console
  takes its lock back at its next check, every 30 seconds; if another console took it in the meantime, this one stops
  rather than act twice. The SQLite lock was tried with two consoles on one data directory and with the holder killed;
  the PostgreSQL lock has not run against a real server yet, and its check,
  [`tests/live/check_store_lock.py`](../tests/live/check_store_lock.py), is ready for the next lab run.
* An older console refuses a state store that a newer one has written.
* `/readyz` checks the store, and the image's health check uses it; the systemd unit restarts a console that exits.
* A webhook that fails, or answers with a server error or `429`, is tried three times (after 2 and 8 seconds); one that
  still fails is in the audit log as `notification.failed`.

Missing:

* **Failover.** If the console is down, nothing is collected and no incident moves until it is back (a host agent keeps
  working on its own host).
* **Schema migrations.** 0.2.0 is the console's first release, so there is nothing to migrate yet; a release that
  changes the store will need them. Back up before every upgrade.
* A **restore** from a backup has not been rehearsed.

## Scale

[`tests/bench_metric_store.py`](../tests/bench_metric_store.py) on an Apple M4 with the default SQLite store, data in the
page cache (`python -m tests.bench_metric_store [servers] [metrics]`). The real servers in the lab reported 68 to 78
values per collection.

| | 80 metrics a server | 50 metrics a server |
|---|---|---|
| One collection round of 300 servers | 47 ms | 41 ms |
| Roll-ups of a week of one server (10,080 samples) | 403 ms | 260 ms |
| Chart read, 6 metrics over 1 h, 24 h, 7 days | 1.4, 27.5, 36.0 ms | 1.0, 18.7, 18.0 ms |
| Latest values of 300 servers (the estate page) | 0.2 ms | 0.2 ms |
| On disk: a sample, a 15-minute roll-up | 4,127 and 4,626 bytes | 2,072 and 4,126 bytes |
| At the default retention: 3 days of samples a minute apart, 93 days of roll-ups | 59 MB a server, 17.7 GB for 300 | 46 MB a server, 13.7 GB for 300 |

What it means:

* Storing is not the limit: a round of 300 servers takes under 0.5% of the shortest collection interval (10 seconds).
* Disk is. Size the store for about 60 MB a server at the defaults; the roll-up retention (Settings → General) is the
  lever, since roll-ups take 70 to 80% of it.
* The chart reads were measured on a week written in one go. In a live store the samples of many servers interleave,
  so a read from a cold cache touches more pages.

Other limits: at most 3 AI sessions run at once by default (`RAIDEN_MAX_AI_SESSIONS`; chat is not counted), the rest
queue. Collection, monitors and incidents run in one process; the lab had 7 real servers, the demo 7 simulated ones.
The 300 above are the metric store's numbers, not 300 live collectors.

## Watching the console

* `/healthz` for liveness, `/readyz` for readiness (the state store answers).
* `/api/v1/prometheus` (with `RAIDEN_METRICS_TOKEN`): every collected metric per server, and the console's own health:
  `raiden_server_up`, `raiden_server_sample_age_seconds`, `raiden_open_incidents`, `raiden_pending_approvals`,
  `raiden_ai_tokens_today`, `raiden_ai_errors_today` and `raiden_console_info`. Alert on a server that stops answering,
  on numbers older than three collection intervals, and on failed model calls.
* Logs as text, or one JSON object per line for a log pipeline (`RAIDEN_LOG_FORMAT=json`).
* The audit log, in the UI and the API.

Missing: tracing, and the audit log has no retention setting yet (it only grows; its rows are small).

## Accessibility

[`web/scripts/a11y.mjs`](../web/scripts/a11y.mjs) (`make a11y`) runs axe-core 4.14.0, the engine of Lighthouse's
accessibility audit, with the WCAG 2.1 A and AA rules on 19 pages in the light and the dark theme: the sign-in page,
every page of the console, an open incident and a resolved one. It finds no violations. An earlier run on 16 of these
pages, before the fixes, failed on 905 elements. Keyboard focus is always visible, and the decorative animations (the
current on the incident board, the lamp of a gate, the pulse of a server that is down) stop for people who ask for
reduced motion.

Missing: a walkthrough with a screen reader, and the first-run setup page (it only exists before the first account).

## Supply chain

* Python: [`requirements/console.lock`](../requirements/console.lock) pins all 45 packages with their sha256 hashes; the
  image installs it with `--require-hashes` and runs `pip check`. A test fails when `pyproject.toml` changes without
  `make lock`.
* The UI: `package-lock.json` and `npm ci`.
* The image runs as an unprivileged user (uid 10001); the systemd unit is sandboxed (`ProtectSystem=strict`,
  `NoNewPrivileges`, private `/tmp` and devices).

Missing: an SBOM, a signed image with provenance, base images pinned by digest, automated dependency updates and a
vulnerability scan in CI.

## Tests and CI

On every push ([`.github/workflows/tests.yml`](../.github/workflows/tests.yml)): the 196 tests of the host agent and the
console, none of which needs PostgreSQL or a model (API, autonomy policy, SQL guard, assessment, the driver against a
fake connection, metric store, sign-in throttling, headers, the store lock, learning and dreams; the end-to-end incident
on the demo fleet, including the next one going through the gate on a learned approval, runs in a job of its own), ruff
on the console, the UI's typecheck and build, and the image with a smoke test (readiness, the demo, the
Content-Security-Policy). The checks against real servers ([`tests/live/`](../tests/live)) run on a lab VM by hand.

Missing: browser tests and the accessibility check in CI.

## AI: cost, control and data

* **Cost.** Each AI endpoint has a daily token budget and a monthly cost budget; routing per purpose has fallbacks; every
  call is counted per incident, server and purpose. The real incident in the lab used 82,167 tokens with `gpt-6.1-sol`,
  from detection to the report; yours will depend on the model and the incident.
* **Control.** The model reads with read-only SQL behind the guard and changes things only through typed actions; SQL
  that writes always waits for a person. `RAIDEN_MAX_AI_SESSIONS` caps how many sessions run at once.
* **Data.** The model sees what its read-only tools return: metrics, settings, object and role names, the assessment,
  and statement text cut to 300 characters. `pg_stat_statements` replaces constants with placeholders, but a running
  statement in `pg_stat_activity` carries its literals. A host agent also reads the PostgreSQL log. Choose an endpoint
  inside your data boundary: Azure OpenAI in your own tenant, or a self-hosted OpenAI-compatible server (vLLM, Ollama).

Missing: **redaction** of literals in statement text before it reaches the model.

## Before you point it at production

1. Set `RAIDEN_SECRET_KEY` from your secret store, and back it up with the state store.
2. Serve the console over HTTPS behind your reverse proxy (`RAIDEN_BASE_URL=https://...`) and keep it off the internet;
   until single sign-on lands, put an identity-aware proxy in front.
3. Use a PostgreSQL state store, or back up the SQLite file; size it for about 60 MB a server.
4. Create each server's login with the onboarding SQL: `pg_monitor`, plus only the grants for the actions you want.
5. Add a staging server first. Keep production's Remediate and Harden gates on approval until you have watched rAIden
   work on your workload.
6. Set the daily token and monthly cost budgets of every AI endpoint.
7. Turn on the Prometheus endpoint and alert on `raiden_server_up`, `raiden_server_sample_age_seconds` and `/readyz`.
8. Send *A decision is waiting for you* to the on-call channel, and watch the audit log for `notification.failed`.
9. Run one console per state store under a restart policy: the systemd unit, Docker's `restart`, or a Kubernetes
   Deployment with the `Recreate` strategy (a rolling update would start the new console while the old one holds the
   store).

## What comes next

1. Schema migrations, before the first release that changes the store.
2. Single sign-on (OIDC) and roles per environment.
3. Validation on managed services, Azure Database for PostgreSQL first (its metrics and security assessments), and TLS
   to the database servers.
4. A standby console that takes over the store lock when the active one stops.
5. Redaction of literals in the statement text sent to the model.
6. An SBOM, signed images, automated dependency updates, and the browser and accessibility checks in CI.
