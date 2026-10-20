import asyncio
import json

from raiden.agent import CONTEXT_LIMIT_CHARS, _prune, run_session
from raiden.events import AuditLog, EventLog
from raiden.guardrails import Guard
from raiden.tools import ToolBox
from tests.fakes import FakeLLM, call


def setup(cfg, steps):
    idir = cfg.incidents_dir / "inc-a"
    idir.mkdir(parents=True)
    events = EventLog(cfg.events_path)
    box = ToolBox(cfg, Guard(cfg), AuditLog(cfg.audit_path), events, "inc-a", idir)
    return FakeLLM(steps), box, idir, events


def drive(cfg, llm, box, idir, events, max_turns=10):
    return asyncio.run(run_session(cfg, llm, box, "SYSTEM", "TASK", max_turns=max_turns, timeout_s=30, transcript_path=idir / "transcript.jsonl", events=events, kind="incident"))


def test_session_runs_tools_and_finishes(cfg):
    llm, box, idir, ev = setup(cfg, [
        [call("bash", command="echo diagnosing")],
        [call("write_file", path="$RAIDEN_INCIDENT_DIR/report.md", content="# report")],
        [call("finish", resolution="resolved", summary="fixed it")],
    ])
    res = drive(cfg, llm, box, idir, ev)
    assert (res.resolution, res.summary, res.turns, res.tool_calls, res.denied_calls) == ("resolved", "fixed it", 3, 3, 0)
    assert (res.prompt_tokens, res.completion_tokens, res.cached_tokens) == (300, 60, 30)
    assert (idir / "report.md").read_text() == "# report"
    transcript = [json.loads(x) for x in (idir / "transcript.jsonl").read_text().splitlines()]
    assert [r["role"] for r in transcript] == ["assistant", "tool", "assistant", "tool", "assistant", "tool"]
    tools = [json.loads(x) for x in cfg.events_path.read_text().splitlines() if '"agent_tool"' in x]
    assert [t["tool"] for t in tools] == ["bash", "write_file", "finish"]


def test_a_denial_reaches_the_model_and_it_adapts(cfg):
    llm, box, idir, ev = setup(cfg, [
        [call("bash", command="pkill -f postgres")],
        [call("bash", command="echo using a gentler route")],
        [call("finish", resolution="mitigated", summary="ok")],
    ])
    res = drive(cfg, llm, box, idir, ev)
    assert res.denied_calls == 1 and res.resolution == "mitigated"
    tool_msg = llm.seen[1][-1]
    assert tool_msg["role"] == "tool" and tool_msg["content"].startswith("DENIED by guardrail [no-mass-kill]")


def test_invalid_json_arguments_do_not_crash_the_loop(cfg):
    llm, box, idir, ev = setup(cfg, [[call("bash", **{})], ])
    llm.steps = [[{"name": "bash", "args": "{not json"}], [call("finish", resolution="escalated", summary="x")]]
    res = drive(cfg, llm, box, idir, ev)
    assert "not valid JSON" in llm.seen[1][-1]["content"] and res.resolution == "escalated"


def test_a_plain_reply_is_nudged_once(cfg):
    llm, box, idir, ev = setup(cfg, ["I believe I am done.", [call("finish", resolution="resolved", summary="s")]])
    res = drive(cfg, llm, box, idir, ev)
    assert res.resolution == "resolved"
    assert "call `finish` now" in llm.seen[1][-1]["content"]
    llm2, box2, idir2, ev2 = setup(cfg.__class__(**{**cfg.__dict__, "state_dir": cfg.state_dir / "two"}), ["done", "still done"])
    res2 = drive(cfg, llm2, box2, idir2, ev2)
    assert res2.resolution is None and res2.summary == "still done"


def test_turn_limit(cfg):
    llm, box, idir, ev = setup(cfg, [[call("bash", command="true")]] * 10)
    res = drive(cfg, llm, box, idir, ev, max_turns=3)
    assert res.turns == 3 and res.resolution is None and not res.timed_out


def test_model_errors_are_reported(cfg):
    class Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("LLM unavailable after retries: 429")

    _, box, idir, ev = setup(cfg, [])
    res = drive(cfg, Boom(), box, idir, ev)
    assert res.resolution is None and "LLM unavailable" in res.error


def test_prune_elides_old_tool_output_but_keeps_the_recent_ones():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for i in range(30):
        msgs += [{"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": f"{i}" * 30_000}]
    _prune(msgs)
    total = sum(len(str(m.get("content") or "")) for m in msgs)
    assert total <= CONTEXT_LIMIT_CHARS + 30_000
    assert msgs[3]["content"] == "[earlier output elided]"
    assert msgs[-1]["content"].startswith("29")
