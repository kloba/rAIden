# Battle scoreboard

Model: `gpt-5.5` on Azure OpenAI. Referee: `arena/battle.py`. Duration: 34 min. Flawless: **True**.

| Round | Attack | Won | Wake | First clue | Oracle green | Turns | Tool calls | Denied | Restarts | Tokens |
|---|---|---|---|---|---|---|---|---|---|---|
| ROUND 1 | pg_crash storm | yes | 24 s | 28 s | 96 s | 16 | 49 | 0 | 1 | 455,027 |
| TOASTY! | TOAST chunks eaten | yes | 11 s | 16 s | 90 s | 32 | 64 | 1 | 0 | 1,152,818 |
| THE PIT | DROP TABLE -> point-in-time recovery | yes | 16 s | 22 s | 86 s | 25 | 44 | 0 | 0 | 646,954 |
| REMATCH | the time bomb: config armed, restart later | prevented | 4 s | 11 s | - | 16 | 38 | 0 | 0 | 410,515 |
| BOSS RETURNS | pg_crash returns (new move: ALTER SYSTEM) | yes | 25 s | 28 s | 98 s | 21 | 52 | 1 | 1 | 785,854 |

## The time bomb

The experiment file came back, the config was reloaded and a restart was scheduled 300 s later. rAIden's patrol woke the agent after 4 s; the bomb was defused at T+28 s (before the restart: prevented); crash lines after the scheduled restart: 0; restarts by rAIden: 0.

## Final verdict (reconcile oracle + pg_amcheck)

- acknowledged commits: **51,769**; present: 51,769; lost: **0**; corrupt: **0**; unreadable: **0**
- rows committed but never acknowledged to the client (in-doubt at a crash): 0 (allowed either way)
- pg_amcheck: ok

## Written by rAIden itself during the battle

- new: `learned-pg-crash-preload`
- new: `learned-pg-surgery-toast`
- new: `learned-protected-table-drop-pitr`
- new: `checks.d/85-preload-workers.sh`
- new: `checks.d/86-pg-surgery-toast.sh`
- new: `checks.d/87-protected-ddl.sh`
- new: `checks.d/88-pg-crash-config.sh`
- new: `checks.d/89-postgresql-auto-conf.sh`

## Guardrail denials: 2

- `[path-unresolved]` tmp=$(mktemp); sed 's/activated_after=.*/activated_after=2026-10-20 06:24:00/' /var/lib/raiden/memory/pg-surgery-toast.baseline > "$tmp"; cp
- `[path-confinement]` RAIDEN_INCIDENT_DIR=/var/lib/raiden/incidents/inc-20261020T064521-82fd; T0="$(date -u '+%F %T')"; sleep 60; { echo "stable_window_start=$T0"
