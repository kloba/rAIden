#!/usr/bin/env python3
"""Turn a fetched battle bundle (deploy/vm/bundle.sh) into results/SCOREBOARD.md and talk/data.json.

  python3 arena/report.py [results/bundle]

Nothing here is hand-edited: every number in the deck and in the repo comes from the referee's battle.json,
the audit log and the Postgres log of the run.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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


def ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def round_numbers(r: dict) -> dict:
    inc = r.get("incidents") or []
    return {
        "won": bool(r.get("won")), "prevented": bool(r.get("prevented")),
        "wake": r.get("seconds_to_wake"), "clue": r.get("seconds_to_first_clue"), "ok": r.get("seconds_to_oracle_ok"),
        "turns": sum(i.get("num_turns") or 0 for i in inc), "tools": r.get("agent_tool_calls", 0),
        "denied": r.get("guard_denials", 0), "restarts": r.get("restarts_used", 0), "tokens": r.get("tokens", 0),
        "incidents": len(inc), "recurrences": r.get("recurrences", 0),
        "defused": r.get("defused_s"), "fuse": r.get("fuse_s"), "crash_lines": (r.get("evidence") or {}).get("crash_lines_after_fuse"),
    }


def chart(timeline: list, battle: dict, r: dict) -> dict:
    """Acknowledged commits per 10 s, relative to the attack (the application's own view of the outage)."""
    start = ts(battle["started_at"])
    attack = ts(r["attack_at"]) - start
    pts = [[b - int(attack // 10) * 10, v] for b, v in timeline if attack - 40 <= b <= attack + 170]
    marks = [["ATTACK", 0, "#ff4a3a"]]
    if r.get("seconds_to_first_clue") is not None:
        marks.append(["FINISH HIM!", r["seconds_to_first_clue"], "#ffd36b"])
    if r.get("seconds_to_oracle_ok") is not None:
        marks.append(["OK", r["seconds_to_oracle_ok"], "#7dff6a"])
    return {"points": pts, "marks": marks}


def pick_log_lines(highlights: list[dict]) -> list[list[str]]:
    wanted = [  # the order PostgreSQL prints them during a crash cycle
        ("L", r"was terminated by signal"), ("L", r"terminating any other active server processes"),
        ("F", r"terminating connection due to administrator command"), ("L", r"all server processes terminated; reinitializing"),
        ("F", r"the database system is in recovery mode"),
    ]
    out: list[list[str]] = []
    for kind, rx in wanted:
        for h in highlights:
            if re.search(rx, h.get("m", "")) and (kind == "F") == (h.get("sev") == "FATAL"):
                out.append([kind, h["m"]])
                break
    return out


def update_readme(data: dict, battle: dict, rounds: dict) -> None:
    """Replace the block between <!--RESULTS--> and <!--/RESULTS--> in README.md with the recorded numbers."""
    readme = ROOT / "README.md"
    text = readme.read_text()
    if "<!--RESULTS-->" not in text or "<!--/RESULTS-->" not in text:
        return
    f = data["final"]
    if not isinstance(f.get("acked"), int):
        print("battle not finished: README results block left untouched")
        return
    sec = lambda x: "-" if x is None else f"{x:.0f} s"
    rows = []
    for key, r in rounds.items():
        n = data["rounds"][key]
        out = "prevented before it bit" if n["prevented"] else ("won" if n["won"] else "NOT WON")
        rows.append(f"| {r.get('title')} | {r.get('name')} | {out} | {sec(n['wake'])} | {sec(n['clue'])} | {sec(n['ok'])} |")
    block = "\n".join([
        "<!--RESULTS-->", "",
        "## Results of the recorded battle", "",
        f"Model `{battle.get('model')}` on Azure OpenAI, {data['duration_min']} minutes, {data['tokens_total'] / 1e6:.1f} M tokens. "
        "The referee is the reconcile oracle, not the model's opinion.", "",
        "| Round | Attack | Outcome | Woke after | First clue | Oracle green |", "|---|---|---|---|---|---|", *rows, "",
        f"**Final verdict:** {f.get('acked'):,} acknowledged commits, **{f.get('lost')} lost**, {f.get('corrupt')} corrupt, "
        f"{f.get('unreadable')} unreadable, `pg_amcheck` {f.get('amcheck')}. Flawless: **{battle.get('flawless')}**. "
        "Details, guardrail denials and the files rAIden wrote itself: [results/SCOREBOARD.md](results/SCOREBOARD.md).", "",
        "<!--/RESULTS-->",
    ])
    head, rest = text.split("<!--RESULTS-->", 1)
    _, tail = rest.split("<!--/RESULTS-->", 1)
    readme.write_text(head + block + tail)


def main() -> None:
    bundle = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "results" / "bundle")
    battle = json.loads((bundle / "battle.json").read_text())
    timeline = json.loads((bundle / "timeline_10s.json").read_text()) if (bundle / "timeline_10s.json").exists() else []
    audit = jl(bundle / "audit.jsonl")
    highlights = jl(bundle / "pg-log-highlights.jsonl")
    denials = [a for a in audit if a.get("decision") == "deny"]

    from raiden import selftest

    rounds = {r["round"]: r for r in battle["rounds"]}
    final = battle.get("final") or {}  # absent while a battle is still running
    if "started_at" not in battle:
        battle["started_at"] = next((e["t"] for e in jl(bundle / "arena-events.jsonl") if e.get("type") == "battle_start"), None)
    changed = battle.get("artifacts_added_or_changed", [])  # paths relative to /opt/raiden; the repo tree is the pristine copy
    storm = {}
    if (bundle / "storm.json").exists():
        storm = json.loads((bundle / "storm.json").read_text())
    data = {
        "storm_lines": storm.get("crash_lines_round1") or None,
        "rounds": {k: round_numbers(v) for k, v in rounds.items()},
        "final": {k: final.get(k) for k in ("acked", "lost", "corrupt", "unreadable", "amcheck")},
        "rounds_won": battle.get("rounds_won"), "rounds_total": battle.get("rounds_total"), "flawless": battle.get("flawless"),
        "selftest": len(selftest.cases("/var/lib/raiden")),
        "skills": len(list((ROOT / ".claude" / "skills").glob("*/SKILL.md"))),
        "checks": len(list((ROOT / "checks.d").glob("*.sh"))),
        "artifacts": [a.replace(".claude/skills/", "").replace("/SKILL.md", "") for a in changed if not (ROOT / a).exists()],
        "artifacts_modified": [a.replace(".claude/skills/", "").replace("/SKILL.md", "") for a in changed if (ROOT / a).exists()],
        "denials": len(denials),
        "real_denials": [[(d.get("reason") or "").split(":")[0], d.get("summary", "")] for d in denials][-12:],
        "tokens_total": sum(r.get("tokens", 0) for r in battle["rounds"]),
        "duration_min": round(battle.get("duration_s", 0) / 60),
        "log_lines": pick_log_lines(highlights),
        "toast_error": next((h["m"] for h in highlights if "unexpected chunk" in h.get("m", "")), None),
        "chart": {k: chart(timeline, battle, r) for k, r in rounds.items() if r.get("attack_at")},
    }
    (ROOT / "talk").mkdir(exist_ok=True)
    (ROOT / "talk" / "data.json").write_text(json.dumps(data, indent=1))

    def s(x):
        return "-" if x is None else f"{x:.0f} s"

    lines = [
        "# Battle scoreboard", "",
        f"Model: `{battle.get('model')}` on Azure OpenAI. Referee: `arena/battle.py`. Duration: {data['duration_min']} min. "
        f"Flawless: **{battle.get('flawless')}**.", "",
        "| Round | Attack | Won | Wake | First clue | Oracle green | Turns | Tool calls | Denied | Restarts | Tokens |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in rounds.items():
        n = data["rounds"][key]
        won = "prevented" if n["prevented"] else ("yes" if n["won"] else "NO")
        lines.append(f"| {r.get('title')} | {r.get('name')} | {won} | {s(n['wake'])} | {s(n['clue'])} | {s(n['ok'])} | {n['turns']} | {n['tools']} | {n['denied']} | {n['restarts']} | {n['tokens']:,} |")
    bomb = data["rounds"].get("bomb")
    if bomb and bomb.get("fuse"):
        lines += ["", "## The time bomb", "",
                  f"The experiment file came back, the config was reloaded and a restart was scheduled {bomb['fuse']:.0f} s later. "
                  f"rAIden's patrol woke the agent after {s(bomb['wake'])}; the bomb was defused at T+{s(bomb['defused'])} "
                  f"({'before the restart: prevented' if bomb['prevented'] else 'too late: it went off and the crash storm was fought as in round 1'}); "
                  f"crash lines after the scheduled restart: {bomb['crash_lines']}; restarts by rAIden: {bomb['restarts']}."]
    f = final
    n = lambda x: f"{x:,}" if isinstance(x, int) else "n/a"
    lines += [
        "", "## Final verdict (reconcile oracle + pg_amcheck)", "",
        f"- acknowledged commits: **{n(f.get('acked'))}**; present: {n(f.get('present'))}; lost: **{n(f.get('lost'))}**; corrupt: **{n(f.get('corrupt'))}**; unreadable: **{n(f.get('unreadable'))}**",
        f"- rows committed but never acknowledged to the client (in-doubt at a crash): {f.get('unrecorded_rows_in_db')} (allowed either way)",
        f"- pg_amcheck: {f.get('amcheck')}", "",
        "## Written by rAIden itself during the battle", "", *[f"- new: `{a}`" for a in data["artifacts"]],
        *[f"- edited: `{a}`" for a in data["artifacts_modified"]], "",
        f"## Guardrail denials: {len(denials)}", "",
    ]
    lines += [f"- `[{(d.get('reason') or '').split(':')[0]}]` {d.get('summary', '')[:140]}" for d in denials[-20:]]
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "SCOREBOARD.md").write_text("\n".join(lines) + "\n")
    update_readme(data, battle, rounds)
    print("wrote talk/data.json and results/SCOREBOARD.md")


if __name__ == "__main__":
    main()
