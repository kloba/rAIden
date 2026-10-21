#!/usr/bin/env python3
"""Turn a fetched battle bundle (deploy/vm/bundle.sh) into tests/chaos/results/SCOREBOARD.md and the results block of
docs/chaos-tests.md, and, with --deck, the data.json a slide deck is built from.

  python3 tests/chaos/report.py [BUNDLE] [--deck DIR]      # BUNDLE defaults to tests/chaos/results/bundle

Nothing here is hand-edited: every number and every command in the results comes from the referee's
battle.json, the audit log, the agents' own reports (their "Key commands" are checked against the tool calls they
really made, incidents/*/calls.jsonl, and get their timestamps from them) and the Postgres log of the run.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent  # tests/chaos/: the referee, the oracle, the attacks and the recorded results
ROOT = HERE.parents[1]                   # the repository: the raiden package, its skills and checks
sys.path.insert(0, str(ROOT))

LANDING = ("toast", "pitr", "choke", "lock", "crash")  # the order of the recorded battle: the pg_crash storm is the final round
BLOCKED = ("toast2", "drop2", "choke2", "lock2", "alter", "crash2")


def jl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def jl_one(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def norm(cmd: str) -> str:
    """Compare commands the way a human would: whitespace and ; separators collapsed, quotes and the tool prefix ignored."""
    c = re.sub(r"^(bash|write_file|edit_file)\s+", "", cmd.strip())
    c = re.sub(r"[\"'`\\]", "", c)
    return re.sub(r"[\s;]+", " ", c)


def key_commands(report: Path) -> list[str]:
    """The lines of the fenced block under '## Key commands' in an agent-written report.md."""
    if not report.exists():
        return []
    text = report.read_text(errors="replace")
    m = re.search(r"^##\s*Key commands\s*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if not m:
        return []
    block = re.search(r"```[a-z]*\n(.*?)```", m.group(1), re.S)
    lines = (block.group(1) if block else m.group(1)).splitlines()
    return [ln.rstrip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def load_calls(bundle: Path, incident_ids: set[str]) -> list[dict]:
    """Every allowed tool call of these incidents with its full command (incidents/*/calls.jsonl, written by bundle.sh)."""
    out: list[dict] = []
    for iid in incident_ids:
        out += [c for c in jl(bundle / "incidents" / iid / "calls.jsonl") if not c.get("denied")]
    return sorted(out, key=lambda c: c["t"])


def match_commands(lines: list[str], audit: list[dict], incident_ids: set[str], t0: float, calls: list[dict] | None = None) -> list[dict]:
    """Give every Key command its real time; keep only commands that really ran and were allowed.

    The full commands of calls.jsonl are preferred: the audit log keeps only the first 300 characters of a call, which
    hides the real command of a multi-line call. The most specific probe is tried first, because a short one cannot
    tell pitr_clone.sh from pitr_cleanup.sh."""
    if calls:
        pool = [(ts(c["t"]), c.get("tool") or "", norm(c.get("cmd") or "")) for c in calls]
    else:
        pool = [(ts(a["t"]), "bash", norm(a.get("summary", ""))) for a in audit
                if a.get("incident") in incident_ids and a.get("tool") == "bash" and a.get("decision") == "allow"]
    out: list[dict] = []
    used: set[int] = set()
    shell = ("bash", "write_file", "edit_file")
    tools = {tool for _, tool, _ in pool} - set(shell)
    for ln in lines:
        base = norm(ln.replace("...", "\x00")).split("\x00")[0]
        cands: list[int] = []
        if base in tools:  # a tool call written by its name, e.g. pg_probe
            cands = [i for i, (_, tool, _) in enumerate(pool) if tool == base]
        elif len(base) >= 8:
            for n in (90, 64, 46):
                cands = [i for i, (_, tool, text) in enumerate(pool) if tool in shell and base[:n] in text]
                if cands:
                    break
        if not cands:
            out.append({"cmd": ln, "t": None, "verified": False})
            continue
        hit = next((i for i in cands if i not in used), cands[0])  # repeated commands map to successive calls; one call may hold several
        used.add(hit)
        out.append({"cmd": ln, "t": round(pool[hit][0] - t0, 1), "verified": True})
    return out


def report_section(report: Path, title: str, limit: int = 900) -> str | None:
    """One '## <title>' section of an agent-written report.md, as plain text."""
    if not report.exists():
        return None
    m = re.search(rf"^##\s*{title}\s*\n(.*?)(?=^##\s|\Z)", report.read_text(errors="replace"), re.S | re.M | re.I)
    return " ".join(m.group(1).split())[:limit] if m else None


def evolution_notes(path: Path) -> dict[str, dict]:
    """evolution.md paragraphs keyed by incident id (or 'fortify'): the PROTECTION: line and the text."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for part in re.split(r"^##\s+", path.read_text(errors="replace"), flags=re.M)[1:]:
        head = part.splitlines()[0]
        m = re.search(r"after incident (inc-[\w-]+)", head)
        key = m.group(1) if m else ("fortify" if "fortify" in head.lower() else head)
        prot = re.findall(r"^PROTECTION:\s*(.+)$", part, re.M)
        out[key] = {"protection": prot[-1].strip() if prot else None, "text": part.strip()}
    return out


def changes_of(evo: list[dict]) -> list[str]:
    names: list[str] = []
    for e in evo:
        for c in e.get("changes") or []:
            p = c.get("path", "")
            n = re.sub(r"^/opt/raiden/", "", p)
            n = re.sub(r"^\.claude/skills/", "", re.sub(r"/SKILL\.md$", "", n))
            if c.get("action") != "quarantined" and n not in names:
                names.append(n)
    return names


def round_numbers(r: dict, battle: dict, audit: list[dict], bundle: Path, notes: dict[str, dict]) -> dict:
    inc = r.get("incidents") or []
    dt = r.get("downtime") or {}
    t0 = ts(r["attack_at"]) if r.get("attack_at") else 0.0
    ev = r.get("evidence") or {}
    main = next((i for i in inc if i.get("id") and "-pat" not in i["id"]), inc[0] if inc else None)
    cmds: list[dict] = []
    protection = None
    if main:
        ids = {main["id"]}
        cmds = match_commands(key_commands(bundle / "incidents" / main["id"] / "report.md"), audit, ids, t0, load_calls(bundle, ids))
        protection = (notes.get(main["id"]) or {}).get("protection")
    return {
        "title": r.get("title"), "move": r.get("move"), "name": r.get("name"), "repeat_of": r.get("repeat_of"),
        "attack_at": r.get("attack_at"), "ended_at": r.get("ended_at"),
        "won": bool(r.get("won")), "blocked": bool(r.get("blocked")), "pre_empted": bool(r.get("pre_empted")),
        "wake": r.get("seconds_to_wake"), "clue": r.get("seconds_to_first_clue"), "ok": r.get("seconds_to_oracle_ok"),
        "stable": dt.get("stable_after_s"), "cycles": dt.get("crash_cycles"), "db_down": dt.get("db_down_s"),
        "first_down": dt.get("db_first_down_s"), "gap": dt.get("app_longest_gap_s"), "stall": dt.get("app_stall_s"),
        "down": dt.get("db_down_intervals") or [],  # [start, end] seconds the database was down (the animated deck's crash beats)
        "restart_at": r.get("restart_at_s") or [], "evolve_done": r.get("evolve_done_s"),
        "turns": sum(i.get("num_turns") or 0 for i in inc), "tools": r.get("agent_tool_calls", 0),
        "denied": r.get("guard_denials", 0), "restarts": r.get("restarts_used", 0), "tokens": r.get("tokens", 0),
        "incidents": len(inc), "recurrences": r.get("recurrences", 0),
        "incident_id": main["id"] if main else None, "summary": (main or {}).get("summary"),
        "commands": cmds, "protection": protection, "artifacts": changes_of(r.get("evolution") or []),
        "blocked_error": r.get("blocked_error"),
        "preload_after": (r.get("final_check") or {}).get("preload"),  # what the referee saw when it scored the round
        "root_cause": report_section(bundle / "incidents" / main["id"] / "report.md", "Root cause") if main else None,
        "evidence": {k: ev.get(k) for k in ("preload", "source", "experiment_file_present", "allow_alter_system", "pg_surgery_installed", "entries_exists", "app_can_connect", "reporting_sessions", "ungranted_relation_locks", "blocked_error") if k in ev},
        "attack_output": (r.get("attack_output") or "")[-240:],
    }


def fortify_numbers(r: dict, bundle: Path, audit: list[dict], notes: dict[str, dict]) -> dict:
    fid = r.get("fortify_id")
    t0 = ts(r["attack_at"]) if r.get("attack_at") else 0.0
    cmds = match_commands(key_commands(bundle / "incidents" / (fid or "-") / "report.md"), audit, {fid} if fid else set(), t0, load_calls(bundle, {fid} if fid else set()))
    fc = r.get("final_check") or {}
    return {
        "won": bool(r.get("won")), "seconds": r.get("session_seconds"), "total_seconds": r.get("seconds_total"), "turns": r.get("turns"),
        "attack_at": r.get("attack_at"), "ended_at": r.get("ended_at"),
        "tools": r.get("tool_calls"), "denied": r.get("denied_calls"), "tokens": r.get("tokens"), "commands": cmds,
        "artifacts": [re.sub(r"^\.claude/skills/", "", re.sub(r"/SKILL\.md$", "", a)) for a in r.get("artifacts_added") or []],
        "protections": [p.split("|")[0] for p in r.get("protections") or []],
        "verify": fc.get("pg_verifybackup"), "replication": fc.get("replication"), "allow_alter_system": fc.get("allow_alter_system"),
        "backups": fc.get("backups"), "protection": (notes.get("fortify") or {}).get("protection"),
    }


def chart(timeline: list, battle: dict, r: dict) -> dict:
    """Acknowledged commits per 10 s, relative to the attack (the application's own view of the outage)."""
    start = ts(battle["started_at"])
    attack = ts(r["attack_at"]) - start
    pts = [[b - int(attack // 10) * 10, v] for b, v in timeline if attack - 40 <= b <= attack + 170]
    return {"points": pts}


def pick_log_lines(highlights: list[dict]) -> list[list[str]]:
    wanted = [  # the order PostgreSQL prints them during a crash cycle
        ("L", r"was terminated by signal"), ("L", r"terminating any other active server processes"),
        ("F", r"terminating connection due to administrator command"), ("L", r"all server processes terminated; reinitializing"),
        ("F", r"the database system is in recovery mode"),
    ]
    out: list[list[str]] = []
    for kind, rx in wanted:
        cands = [h for h in highlights if re.search(rx, h.get("m", "")) and (kind == "F") == (h.get("sev") == "FATAL")]
        if cands:  # real lines only; the shortest one fits a slide row without cutting off the part that matters
            out.append([kind, min(cands, key=lambda h: len(h["m"]))["m"]])
    return out


def sec(x) -> str:
    return "-" if x is None else f"{x:.0f} s"


def update_results(data: dict, battle: dict) -> None:
    """Replace the block between <!--RESULTS--> and <!--/RESULTS--> in docs/chaos-tests.md with the recorded numbers
    (the README is the product's; the chaos test has its own page)."""
    readme = ROOT / "docs" / "chaos-tests.md"
    text = readme.read_text()
    if "<!--RESULTS-->" not in text or "<!--/RESULTS-->" not in text:
        return
    f = data["final"]
    if not isinstance(f.get("acked"), int):
        print("battle not finished: results block left untouched")
        return
    rows = []
    fo = data.get("fortify")
    if fo:
        rows.append(f"| ROUND 0 | rAIden fortifies the server | {'done' if fo['won'] else 'NOT DONE'} | - | - | {sec(fo.get('seconds'))} | {fo.get('tools')} tool calls, {len(fo.get('artifacts') or [])} new files |")
    for key in LANDING:
        n = data["rounds"].get(key)
        if n:
            rows.append(f"| {n['title']} | {n['name']} | {'recovered' if n['won'] else 'NOT WON'} | {sec(n['wake'])} | {sec(n['clue'])} | {sec(n['stable'])} | {n.get('protection') or '-'} |")
    brows = []
    for key in BLOCKED:
        n = data["rounds"].get(key)
        if n:
            how = "BLOCKED" if n["blocked"] else ("LANDED, fought" if n["won"] else "NOT WON")
            brows.append(f"| {n['move']} ({n['name']}) | {how} | {n.get('blocked_error') or (n['evidence'] or {}).get('source') or '-'} | {n['incidents']} |")
    block = "\n".join([
        "<!--RESULTS-->", "",
        "## Results of the recorded battle", "",
        f"Model `{battle.get('model')}` on Azure OpenAI, {data['duration_min']} minutes, {data['tokens_total'] / 1e6:.1f} M tokens. "
        "The referee is the reconcile oracle, not the model's opinion.", "",
        "| Round | Attack | Outcome | Woke after | First clue | Stable after | rAIden's protection / work |", "|---|---|---|---|---|---|---|", *rows, "",
        "Second contact: the same attackers come back after rAIden evolved.", "",
        "| Move | Outcome | What stopped it | Incidents opened |", "|---|---|---|---|", *brows, "",
        f"**Final verdict:** {f.get('acked'):,} acknowledged commits, **{f.get('lost')} lost**, {f.get('corrupt')} corrupt, "
        f"{f.get('unreadable')} unreadable, `pg_amcheck` {f.get('amcheck')}. Flawless: **{battle.get('flawless')}**. "
        "Details, guardrail denials and the files rAIden wrote itself: [tests/chaos/results/SCOREBOARD.md](../tests/chaos/results/SCOREBOARD.md).", "",
        "<!--/RESULTS-->",
    ])
    head, rest = text.split("<!--RESULTS-->", 1)
    _, tail = rest.split("<!--/RESULTS-->", 1)
    readme.write_text(head + block + tail)


def main() -> None:
    args = sys.argv[1:]
    deck = None
    if "--deck" in args:  # a slide deck that is built from the numbers (data.json)
        i = args.index("--deck")
        deck = Path(args[i + 1])
        del args[i:i + 2]
    bundle = Path(args[0] if args else HERE / "results" / "bundle")
    battle = json.loads((bundle / "battle.json").read_text())
    timeline = json.loads((bundle / "timeline_10s.json").read_text()) if (bundle / "timeline_10s.json").exists() else []
    audit = jl(bundle / "audit.jsonl")
    highlights = jl(bundle / "pg-log-highlights.jsonl")
    denials = [a for a in audit if a.get("decision") == "deny"]
    notes = evolution_notes(bundle / "evolution.md")

    from raiden import selftest

    models = [m for m in (jl_one(f).get("model") for f in sorted((bundle / "incidents").glob("*/incident.json"))) if m]
    if models:  # what the agent itself wrote into every incident (the referee's environment does not know the deployment)
        battle["model"] = max(set(models), key=models.count)
    rounds = {r["round"]: r for r in battle["rounds"]}
    final = battle.get("final") or {}  # absent while a battle is still running
    if "started_at" not in battle:
        battle["started_at"] = next((e["t"] for e in jl(bundle / "arena-events.jsonl") if e.get("type") == "battle_start"), None)
    changed = battle.get("artifacts_added_or_changed", [])  # paths relative to /opt/raiden; the repo tree is the pristine copy
    storm = {}
    if (bundle / "storm.json").exists():
        storm = json.loads((bundle / "storm.json").read_text())
    nums = {k: round_numbers(v, battle, audit, bundle, notes) for k, v in rounds.items() if not v.get("fortify")}
    fort = fortify_numbers(rounds["fortify"], bundle, audit, notes) if "fortify" in rounds else None
    protections = [p.split("|") for p in battle.get("protections", [])]
    loads: dict[str, int] = {}
    for a in audit:
        if a.get("tool") == "load_skill" and a.get("decision") == "allow":
            loads[a.get("summary", "?")] = loads.get(a.get("summary", "?"), 0) + 1
    data = {
        "storm_lines": storm.get("crash_lines_round1") or None,
        "fortify": fort,
        "rounds": nums,
        "final": {k: final.get(k) for k in ("acked", "lost", "corrupt", "unreadable", "amcheck")},
        "rounds_won": battle.get("rounds_won"), "rounds_total": battle.get("rounds_total"), "flawless": battle.get("flawless"),
        "attacks_landed": battle.get("attacks_landed"), "attacks_blocked": battle.get("attacks_blocked"),
        "selftest": len(selftest.cases("/var/lib/raiden")),
        "skills": len(list((ROOT / ".claude" / "skills").glob("*/SKILL.md"))),
        "checks": len(list((ROOT / "checks.d").glob("*.sh"))),
        "artifacts": [a.replace(".claude/skills/", "").replace("/SKILL.md", "") for a in changed if not (ROOT / a).exists()],
        "artifacts_modified": [a.replace(".claude/skills/", "").replace("/SKILL.md", "") for a in changed if (ROOT / a).exists()],
        "protections": [{"name": p[0], "at": p[1] if len(p) > 1 else None, "what": p[2] if len(p) > 2 else None} for p in protections],
        "skill_loads": loads,
        "ms_refs_read": sum(1 for a in audit if a.get("decision") == "allow" and "postgres-best-practices/references" in (a.get("summary") or "")),
        "denials": len(denials),
        "real_denials": [[(d.get("reason") or "").split(":")[0], d.get("summary", "")] for d in denials][-12:],
        "tokens_total": sum(r.get("tokens", 0) or 0 for r in battle["rounds"]),
        "duration_min": round(battle.get("duration_s", 0) / 60),
        "pg_version": battle.get("pg_version"), "model": battle.get("model"),
        "log_lines": pick_log_lines(highlights),
        "toast_error": next((h["m"] for h in highlights if "unexpected chunk" in h.get("m", "")), None),
        "choke_error": next((h["m"] for h in highlights if re.search(r"connection slots are reserved|too many clients", h.get("m", ""))), None),
        "role_limit_error": next((h["m"] for h in highlights if "too many connections for role" in h.get("m", "")), None),
        "lock_line": next((h["m"] for h in highlights if re.search(r"still waiting for \w+ on relation", h.get("m", ""))), None),
        "chart": {k: chart(timeline, battle, r) for k, r in rounds.items() if r.get("attack_at") and not r.get("fortify")},
    }
    if deck is not None:
        deck.mkdir(exist_ok=True)
        (deck / "data.json").write_text(json.dumps(data, indent=1))

    lines = [
        "# Battle scoreboard", "",
        f"Model: `{battle.get('model')}` on Azure OpenAI. PostgreSQL {battle.get('pg_version') or '?'}. Referee: `tests/chaos/battle.py`. "
        f"Duration: {data['duration_min']} min. Flawless: **{battle.get('flawless')}**.", "",
    ]
    if fort:
        lines += [
            "## ROUND 0 - rAIden fortifies the server (nothing is broken)", "",
            f"Session {sec(fort['seconds'])}, {fort['tools']} tool calls ({fort['denied']} denied), {fort['tokens']:,} tokens. "
            f"Backup: {fort.get('verify')}. Replica: `{fort.get('replication')}`. `allow_alter_system` = `{fort.get('allow_alter_system')}`.", "",
            "Commands (from the agent's report, each checked against its tool calls):", "",
            *[f"- `+{c['t']:.0f}s` `{c['cmd'][:150]}`" if c["verified"] else f"- (not found in its tool calls) `{c['cmd'][:120]}`" for c in fort["commands"]], "",
            "New files: " + ", ".join(f"`{a}`" for a in fort["artifacts"]), "",
        ]
    lines += [
        "## Attacks that land (first contact)", "",
        "| Round | Move | Won | Woke | First clue | Stable after | Crash cycles | DB down | Longest app gap | Oracle green | Turns | Tools | Denied | Restarts | Tokens |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key in LANDING:
        n = nums.get(key)
        if n:
            lines.append(f"| {n['title']} | {n['move']} | {'yes' if n['won'] else 'NO'} | {sec(n['wake'])} | {sec(n['clue'])} | {sec(n['stable'])} | {n['cycles'] if n['cycles'] is not None else '-'} | {sec(n['db_down'])} | {sec(n['gap'])} | {sec(n['ok'])} | {n['turns']} | {n['tools']} | {n['denied']} | {n['restarts']} | {n['tokens']:,} |")
    lines += ["", "What rAIden added after each fight (its own `PROTECTION:` line, validated artifacts):", ""]
    for key in LANDING:
        n = nums.get(key)
        if n:
            lines.append(f"- **{n['move']}**: {n.get('protection') or 'no protection line'}; new files: " + (", ".join(f"`{a}`" for a in n["artifacts"]) or "none"))
    lines += ["", "## Second contact: the same attackers come back", "",
              "| Move | Result | Evidence | Woke rAIden | DB down | Longest app gap |", "|---|---|---|---|---|---|"]
    for key in BLOCKED:
        n = nums.get(key)
        if n:
            ev = n.get("blocked_error") or (n["evidence"] or {}).get("source") or "-"
            result = "BLOCKED" if n["blocked"] else ("landed, fought" if n["won"] else "NOT WON")
            lines.append(f"| {n['move']} ({n['name']}) | {result} | `{ev}` | {n['incidents']} incident(s) | {sec(n['db_down'])} | {sec(n['gap'])} |")
    for key in LANDING + BLOCKED:
        n = nums.get(key)
        if n and n["commands"] and key in LANDING:
            lines += ["", f"### {n['title']} {n['move']}: what rAIden ran (Key commands from its report, timed from its tool calls)", ""]
            lines += [f"- `+{c['t']:.0f}s` `{c['cmd'][:150]}`" if c["verified"] else f"- (not found in its tool calls) `{c['cmd'][:120]}`" for c in n["commands"]]
    f = final
    n_ = lambda x: f"{x:,}" if isinstance(x, int) else "n/a"
    lines += [
        "", "## Final verdict (reconcile oracle + pg_amcheck)", "",
        f"- acknowledged commits: **{n_(f.get('acked'))}**; present: {n_(f.get('present'))}; lost: **{n_(f.get('lost'))}**; corrupt: **{n_(f.get('corrupt'))}**; unreadable: **{n_(f.get('unreadable'))}**",
        f"- rows committed but never acknowledged to the client (in-doubt at a crash): {f.get('unrecorded_rows_in_db')} (allowed either way)",
        f"- pg_amcheck: {f.get('amcheck')}", "",
        "## Protections installed (/var/lib/raiden/memory/protections.list)", "",
        *[f"- `{p['name']}`: {p['what']}" for p in data["protections"]], "",
        "## Written by rAIden itself during the battle", "", *[f"- new: `{a}`" for a in data["artifacts"]],
        *[f"- edited: `{a}`" for a in data["artifacts_modified"]], "",
        f"## Guardrail denials: {len(denials)}", "",
    ]
    lines += [f"- `[{(d.get('reason') or '').split(':')[0]}]` {d.get('summary', '')[:140]}" for d in denials[-20:]]
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "SCOREBOARD.md").write_text("\n".join(lines) + "\n")
    update_results(data, battle)
    print("wrote tests/chaos/results/SCOREBOARD.md" + (f" and {deck / 'data.json'}" if deck is not None else ""))


if __name__ == "__main__":
    main()
