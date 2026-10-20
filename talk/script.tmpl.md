# pg_crash vs rAIden: 5-minute speaker script

Lightning talk, PGConf.EU 2026, Valencia. Slides: `rAIden-pgconf-2026.pdf` (10 slides, about 30 s each).
Every number below is filled in from the recorded battle (`results/SCOREBOARD.md`); nothing is typed by hand.
Pace: roughly 135 words per minute, with pauses for the announcer lines. **{words} words, about {minutes_spoken} minutes spoken.**

Delivery: the announcer lines are in CAPS. Say them slowly, deep voice, then drop back to normal. One breath of silence after
FINISH HIM, FATALITY and FLAWLESS VICTORY. No live demo (no laptop switching): the fight was recorded on Azure.

---

## 1 · ROUND 1… FIGHT! (0:00 - 0:20)

(announcer voice, slow) "ROUND ONE… FIGHT!"

(normal) "Notice I did not say *let's* fight. The announcer never does. I'm @kloba. In this corner, an AI DBA that never sleeps:
rAIden. In that corner, pg_crash, the chaos extension from CYBERTEC that sends a random signal to a random Postgres process
every four seconds."

## 2 · Choose your fighter (0:20 - 0:45)

"rAIden reads the Postgres log 24/7 and wakes up in {wake1} seconds. pg_crash has exactly one move: kill, random target, no
mercy. This is not a live demo, because I'm not switching laptops. It happened on Azure last night, and the referee is not
the AI. The referee is a ledger of every commit the application was *told* succeeded."

## 3 · How rAIden fights (0:45 - 1:20)

"Five moves. Watch the log. Triage: {storm} crash lines are *one* incident. Fight: a tool-calling loop, GPT-5.5, and
{skills} runbooks it loads on demand. Verify against the ledger. Evolve: write itself new checks and lessons.

And the referee: guardrails. No pkill, no DROP on application data, never fsync off, a restart budget, every call audited,
{selftest} tests. The model is cooperative, but honest mistakes are how databases die. In the first fight it asked for a
bigger hammer three times, pg_ctlcluster, pg_ctl immediate, systemctl kill minus nine. The referee said no three times."

## 4 · ROUND 1: FINISH HIM! (1:20 - 1:55)

"Round one. Somebody leaves a 'temporary' experiment in conf.d, and pg_crash starts. At T plus {wake1} seconds rAIden wakes up:
one incident, not {storm} log lines. At T plus {clue1} seconds: FINISH HIM! It has found the crash worker in
shared_preload_libraries. The chart is the application's own view, commits per ten seconds. Red is the storm.
{ok1} seconds after the attack the oracle is green."

## 5 · FATALITY (1:55 - 2:20)

"FATALITY! Yes, the Postgres log already prints FATAL. I did not make that up. rAIden commented out one line, restarted once,
and the referee said: zero commits lost. (pause) It is just the first thing that reads the log at 3 a.m."

## 6 · TOASTY! (2:20 - 2:50)

"Round two. TOASTY! (point at the pop-up) TOAST, the Oversized-Attribute Storage Technique. The attacker destroyed the
middle chunks of four fat values. rAIden did not guess: it rebuilt a side clone with point-in-time recovery, checked
the payloads, repaired the rows with a keyed update, and amcheck came back clean. {clue2} seconds to the clue."

## 7 · THE PIT(R) (2:50 - 3:25)

"Final stage: THE PIT. As in Point-In-Time Recovery. Somebody ran DROP TABLE on the ledger. rAIden found the transaction id
in the WAL with pg_waldump, restored a side clone to one tick before the DROP, and brought the table back with its owner and
indexes. {clue3} seconds to the clue. The pit is only a fatality if you have no backups. And a pager."

## 8 · Level up: the time bomb (3:25 - 4:10)

"After every fight rAIden writes itself new moves: {n_art} files this time, checks, runbooks, lessons, each one validated.

So pg_crash came back with a time bomb. The same experiment file, the config reloaded, and the restart that activates it
scheduled {fuse_min} minutes later, in the 'maintenance window'. Nothing is broken yet. A defender that only reads errors
loses here, because there are no errors. But the check rAIden wrote after round one asks a different question: what will
the *next restart* load? At T plus {bomb_wake} seconds it woke up. At T plus {bomb_defused} seconds the bomb was defused, with no restart.
When the scheduled restart fired, nothing happened. That is what 'stronger' means."

## 9 · FLAWLESS VICTORY (4:10 - 4:35)

"FLAWLESS VICTORY. {acked} acknowledged commits, {lost} lost, none corrupt, pg_amcheck OK, {rounds_won} rounds out of
{rounds_total}, including the boss's new move: ALTER SYSTEM beats every file in conf.d, and rAIden still had it in {boss_ok}
seconds. Not rAIden's opinion: the ledger's."

## 10 · CONTINUE? (4:35 - 5:00)

"Continue? Scan the QR. The agent, the guardrails, the referee and the honest bits are in the repo, including the real
PostgreSQL 18 hang that the first run found. Built and fought on Azure, deleted after the fight. No DBAs were harmed. They
got some sleep. Thank you!"

---

## If you are running late (cut in this order)

1. Slide 3: drop the guardrail list, keep only "the model asked for a bigger hammer, the referee said no".
2. Slide 6: say only "TOASTY! Four TOAST chunks destroyed, rebuilt from a point-in-time clone."
3. Slide 8: keep only the last two sentences.

## Questions you will get (honest answers)

* **Why GPT-5.5 and not Claude?** I wanted Claude on Foundry; the sandbox subscription's Marketplace policy refused every
  deployment, so the model had to change. The loop is provider-agnostic (`raiden/llm.py` is about 60 lines).
* **Can it drop my production table?** The guard denies DROP/TRUNCATE/DELETE/UPDATE without WHERE on application data, mass kills,
  pg_resetwal, network, credential reads and durability downgrades. It is a lexical defence against honest mistakes, not a
  sandbox against an adversary; the OS privilege boundary (unprivileged user, narrow sudoers) is the primary control.
* **What if the log contains a prompt injection?** Log text is data, never instructions (it is in the system prompt), and the
  actions are filtered by the guard whatever the model decides. Not a proof; start in a lab.
* **Is one run proof?** No. One run is one sample of a non-deterministic model; the first run found two bugs in my own guard and
  a real PostgreSQL 18.6 hang (fast shutdown during crash recovery), see `results/RUN1-HUNG-POSTMASTER.md`.
* **What did it cost?** {tokens_m} M tokens over {minutes} minutes of fighting.
* **Why not Patroni, pgBackRest, monitoring?** Use them. rAIden does not replace backups or HA; it is the on-call person who reads the
  log at 3 a.m., and it needs the backups to exist (round 3 is only possible because they do).
