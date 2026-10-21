# Security policy

## Reporting a vulnerability

Please report security problems privately: open a draft advisory on GitHub (*Security* → *Report a vulnerability*)
rather than an issue. Include the version, how to reproduce it and what an attacker gains.

## Supported versions

| Version | |
|---|---|
| 0.2.x | security fixes |
| 0.1.x (the host agent on its own) | no longer maintained |

## What rAIden is built to protect

rAIden holds credentials for your databases and can change them, so the console is designed around a few rules. The
README's [Security](README.md#security) section has the details.

* Each server gets a least-privilege login: `pg_monitor`, plus only the grants for the actions you want rAIden to take.
* Questions run in read-only transactions behind a SQL guard; changes are typed actions; high-risk ones always wait for a
  person, and the autonomy policy decides where else a person approves.
* Database passwords and API keys are encrypted at rest with the key from `RAIDEN_SECRET_KEY`; people's passwords are
  hashed with scrypt; host agent tokens are stored as hashes.
* Sign-in is throttled per account and address, and an unknown account answers as slowly as a wrong password.
* Pages carry a Content-Security-Policy that allows scripts only from the console, and cannot be framed.
* The container image installs only hash-pinned dependencies.
* Everything rAIden and people do is in an append-only audit log.

Run the console behind HTTPS (`RAIDEN_BASE_URL=https://...` marks cookies Secure), keep `RAIDEN_SECRET_KEY` in a secret
store, and restrict who can reach the console: it is an operations tool, not a public website. There is no single
sign-on or MFA yet; [docs/production-readiness.md](docs/production-readiness.md) lists this and the other known gaps.
