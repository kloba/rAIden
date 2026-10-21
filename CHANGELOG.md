# Changelog

## 0.2.0 (unreleased)

The release that turns rAIden from a single-server agent into a product for a fleet.

### rAIden Console (new)

* A web console for every PostgreSQL server you run, on-prem or in any cloud: overview (*What needs attention*),
  estate with issue and suggestion chips, fleet performance, a page per server with metrics, sessions and lock trees,
  top queries, tables and vacuum, replication, configuration and assessments.
* 96 metrics, among them all 74 that Azure Monitor defines for PostgreSQL under the same names, from SQL, Azure Monitor
  and the host agent; 23 built-in monitors and your own.
* The incident board: Detected, Triage, Diagnose, Remediate, Verify, Harden, Resolved, with a switch in front of every
  stage that says whether rAIden carries on or a person approves, per environment and per server.
* 15 typed actions behind the approvals, a read-only SQL guard, high-risk actions always for a person, plans and
  protections checked against what rAIden's login may do; changes it may not make go to the server's suggestions.
* Onboarding: a connection test that reads the login's grants, the preparation SQL for each platform, an assessment in
  six areas with a score and sizing from the host profile, one-click fixes through the same approvals. For Azure
  servers, security checks read from the Azure resource (Entra authentication enabled and enforced,
  customer-managed key, public network access).
* Routines on cron with time zones: checks that cost no tokens and AI reviews.
* Chat with read-only tools and inline proposals to approve.
* AI endpoints: Azure OpenAI (Entra ID or key), OpenAI, Anthropic and OpenAI-compatible servers; routing per purpose
  with fallbacks, daily token and monthly cost budgets, token accounting per incident, server and purpose.
* Accounts with admin, operator and viewer roles, an append-only audit log, notifications to Microsoft Teams, Slack or a
  webhook, a Prometheus endpoint, an HTTP API.
* A demo workspace with a simulated fleet and a scripted model.
* Deployment: a container image, docker compose with a PostgreSQL state store, a hardened systemd unit.
* Tested on PostgreSQL 13 to 18 and a streaming standby with a real model: [docs/validation.md](docs/validation.md).

### Running it in production

What it takes to run, and what is still missing: [docs/production-readiness.md](docs/production-readiness.md).

* One console per state store, enforced: a second console refuses to start and says which one holds the store.
* Sign-in throttling (5 failures per account and address, 20 per address, in 15 minutes), and no difference in timing
  between an unknown account and a wrong password.
* A Content-Security-Policy and security headers on every response; HSTS over HTTPS.
* An older console refuses a state store a newer one has written.
* Webhooks are tried three times; a notification that still fails is in the audit log.
* The Prometheus endpoint reports the console's own health: servers that stop answering, the age of their latest
  numbers, the day's tokens and failed model calls.
* JSON logs (`RAIDEN_LOG_FORMAT=json`).
* The image installs hash-pinned dependencies from `requirements/console.lock` (`make lock`) and checks them.
* WCAG 2.1 AA: no axe-core violations on any page in either theme (`make a11y`).
* `python -m tests.bench_metric_store` measures the metric store on your hardware.
* Fixed: `raiden console --data DIR` kept the database in the default data directory.

### Host agent

* Links to the console: heartbeats with host metrics (CPU, memory, disk, network) and the incidents it handles.
* `raiden fortify`: a day-0 hardening session (verified backup, standby, baseline configuration, new checks).

## 0.1.0 (20 October 2026)

The host agent (`raiden run`): it tails the PostgreSQL log, wakes up on crashes, corruption, dropped tables and
configuration time bombs, fixes them behind guardrails, proves the fix and writes itself new checks. Its recorded chaos
test is in [docs/chaos-tests.md](docs/chaos-tests.md).
