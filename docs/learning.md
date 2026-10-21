# How rAIden learns

rAIden gets better at your fleet the longer it works with your team. What people tell it, the reasons they give for their
decisions and the incidents it resolves become **instructions** and **playbooks** that go into every prompt they apply to.
A plan people approve every time stops being a question once an admin accepts that. Once a night it **dreams**: it reads
what is new, keeps what is worth keeping and changes nothing on any server. Everything it learns is on the **Knowledge**
page, with where it came from, and can be edited, retired or undone.

![The Knowledge page: a proposal not to ask again, an inferred rule waiting for a person, the instructions and the dream journal](screenshots/knowledge.png)

## What it knows

| | What it is | Example |
|---|---|---|
| **Rule** | What rAIden does, or never does | Never end sessions of the replication or backup roles. |
| **Fact** | Something true about a server, an application or a role | The migrator role deploys on weekdays at 14:00 UTC. |
| **Preference** | How the team likes things done | Put the exact SQL in every recommendation. |
| **Playbook** | How to handle one kind of incident: what to look at first, the usual root cause, the plan people approve, the protections that worked | Lock queue behind a DDL |

Each has a **scope**: a server, an environment, a kind of incident, or none for the whole fleet. A fact about one server
never reaches a prompt about another. Each says **where it came from** (a person, an incident, a dream and the evidence
it read) and **how long to keep it**: until someone changes it, always, or for 30 days for what is only true for a while.
An edit keeps the version before.

Where it goes:

| Prompt | What it gets |
|---|---|
| An incident, from the start | The instructions for its server and environment: rules first, the narrowest first, at most 14 |
| The diagnosis, once triage named the kind of incident | The instructions for that kind, and its playbook |
| A chat | The instructions for the server in focus, or for the fleet |
| An AI routine | The instructions for the server it runs on |

rAIden is told to name the instruction (by its number) when one decides what it does, and each use is counted, so the
Knowledge page shows what is actually used.

## How it learns

### From what people say

* **Teach rAIden** on the Knowledge page: a sentence, the details, the scope.
* **In a chat**: say *remember that ...*, *from now on ...* or *never ...* and rAIden keeps it for the whole team, scoped to
  the server you are talking about, and shows what it remembered.
* **Notes and reasons**: a note on an incident, a reason given with a rejection, the summary written when a person resolves
  an incident. The next dream reads them.

### From incidents

A lesson rAIden proposes in Harden becomes a fact about that server once the protections are approved.

### From decisions: not asking again

When people approve the same action for the same kind of incident in the same environment several times in a row (3 by
default), rAIden proposes not to ask again. The proposal shows on the Knowledge page, on the incident, and under the next
plan that needs it:

> Approved 3 times in a row by Alex for lock incidents in production: rAIden proposes not to ask again for terminate a session.

Nothing changes until an **admin** accepts, because accepting changes who decides, like the autonomy policy. After that:

* the next plan of that kind goes ahead without waiting. The decision is on the record like any other, made by *learned
  approval #n*, with an event on the timeline, the people who approved it before, and an audit entry;
* only plans whose every action is covered go ahead; a plan with one more action waits for a person;
* high-risk actions are never learned, and a plan longer than the limit in the autonomy policy still waits;
* **the next rejection takes it back**: rAIden asks again, and the count starts over. *Ask me next time* on the incident, or
  on the Knowledge page, does the same without rejecting anything;
* approvals are counted per server when they all came from one server, per environment otherwise, over the last 90 days.

Turn it off, or change the count, under **Autonomy → Stop asking what people always approve**. Off means every learned
approval stops opening gates at once.

### Dreams

The built-in **Dream** routine runs at 03:00 UTC (change it on the Routines page; **Dream now** runs it at once). A dream
reads what happened since the last one: people's decisions and the reasons they gave, their notes, what they said in
chats, the incidents resolved and the suggestions dismissed. Then it makes the smallest honest change that keeps what
rAIden knows coherent:

1. **It learns from people, not from itself.** Every rule, fact or preference cites the decisions, notes, chat messages or
   incidents it comes from. What a person said or wrote becomes active at once; what rAIden only inferred from what people
   did waits on the Knowledge page for a person to confirm it.
2. **It keeps the scope** a thing was said for.
3. **A correction replaces what it corrects**: it updates or retires the older instruction instead of adding a second one.
4. **It does not repeat itself** or what its prompts already say.
5. **It writes a playbook only for a kind of incident fixed at least twice**, and brings it up to date when new incidents
   of that kind come in.
6. **It proposes not to ask again** when a person asked for that in words. What people approved often enough is counted
   and proposed without a model.

The console checks each change, whatever the model says: evidence that is not in the material is refused; what a person
taught, edited or confirmed changes only on a person's own words; a playbook needs a kind of incident fixed twice;
high-risk actions are never proposed. With nothing new to read, a dream makes no model call. It uses the AI endpoint for
routines.

Every dream keeps a journal: what it read, what it changed with the version before, what it cost. An admin can **undo** a
dream; what people changed since stays as they left it.

## Safety

* Instructions shape how rAIden reasons; they never lift a gate. The gates, the typed actions, the high-risk rule and the
  SQL guard are enforced by the console, not by the prompt.
* A dream reads people's words and decisions and what rAIden wrote about incidents, never a server's output itself. An
  instruction that rests only on rAIden's own writing waits for a person; a playbook built from resolved incidents is
  guidance and lifts no gate.
* Every change is in the audit log: `knowledge.create`, `knowledge.update`, `knowledge.delete`, `learned_approval.accept`,
  `learned_approval.decline`, `learned_approval.revoke`, `approval.learned`, `dream`, `dream.undo`.
* Viewers read the Knowledge page. Operators teach, edit, confirm, decline, make rAIden ask again and run a dream. Admins
  accept learned approvals, undo dreams and delete.

## The API

| | |
|---|---|
| `GET /api/v1/knowledge` | Instructions and playbooks, learned approvals, the last 30 dreams, the learning policy |
| `POST /api/v1/knowledge` | Teach: `kind`, `title`, `body`, `scope` (`server`, `environment`, `category`), `retention` |
| `PATCH /api/v1/knowledge/{id}` | Edit, confirm (`status: active`) or retire (`status: retired`); the version before is kept |
| `DELETE /api/v1/knowledge/{id}` | Delete for good (admin) |
| `POST /api/v1/learned-approvals/{id}/accept` | Stop asking (admin) |
| `POST /api/v1/learned-approvals/{id}/decline` | Keep asking |
| `POST /api/v1/learned-approvals/{id}/revoke` | Ask again |
| `POST /api/v1/dreams` | Dream now |
| `GET /api/v1/dreams/{id}` | One dream's journal |
| `POST /api/v1/dreams/{id}/undo` | Undo a dream (admin) |

The dream follows the design of [nanobot's Dream](https://github.com/HKUDS/nanobot/blob/main/docs/memory.md): a
consolidation pass over what is new, the smallest change that keeps memory coherent, versions that can be restored.
