# Lightning talk proposal card (hand-copy onto the card, drop it in the box by Wed 21 Oct 13:35)

**Title:** pg_crash vs rAIden: an AI DBA fights a chaos monkey

**Abstract (card-sized):**
I gave an AI agent, rAIden, the on-call pager of a PostgreSQL 18 lab and let CYBERTEC's pg_crash attack it: random SIGKILLs,
destroyed TOAST chunks, a DROP TABLE and a config time bomb. rAIden reads the log, finds the root cause, fixes it behind hard
guardrails and writes itself new checks afterwards. An independent referee counts every acknowledged commit. ROUND 1... FIGHT!
Recorded on Azure (no live demo, no sales pitch): the code is open source (MIT).

**Format:** 5 minutes, slides only (PDF), Mortal Kombat announcer style.
**Speaker:** @kloba
