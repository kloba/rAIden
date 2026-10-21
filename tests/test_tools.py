import asyncio
import json

import pytest

from raiden.events import AuditLog, EventLog
from raiden.guardrails import Guard
from raiden.tools import TOOL_SPECS, ToolBox, clip


@pytest.fixture
def box(cfg):
    idir = cfg.incidents_dir / "inc-t"
    idir.mkdir(parents=True)
    return ToolBox(cfg, Guard(cfg), AuditLog(cfg.audit_path), EventLog(cfg.events_path), "inc-t", idir)


def run(box, tool, **args):
    return asyncio.run(box.call(tool, args))


def test_tool_specs_are_wellformed():
    names = [t["function"]["name"] for t in TOOL_SPECS]
    assert names == ["bash", "read_file", "write_file", "edit_file", "load_skill", "pg_probe", "escalate", "finish"]
    for t in TOOL_SPECS:
        assert t["function"]["parameters"]["type"] == "object"


def test_write_read_edit_roundtrip_with_incident_variable(box):
    assert "wrote" in run(box, "write_file", path="$RAIDEN_INCIDENT_DIR/notes.md", content="alpha\nbeta\nbeta\n")
    assert (box.incident_dir / "notes.md").read_text().startswith("alpha")
    out = run(box, "read_file", path="${RAIDEN_INCIDENT_DIR}/notes.md")
    assert "    1  alpha" in out
    assert "occurs 2 times" in run(box, "edit_file", path="$RAIDEN_INCIDENT_DIR/notes.md", old="beta", new="gamma")
    assert "1 replacement" in run(box, "edit_file", path="$RAIDEN_INCIDENT_DIR/notes.md", old="alpha", new="ALPHA")
    assert "not found" in run(box, "edit_file", path="$RAIDEN_INCIDENT_DIR/notes.md", old="zzz", new="y")


def test_bash_runs_and_reports_exit_codes(box):
    assert run(box, "bash", command="echo hello; exit 3").endswith("hello\n[exit 3]")


def test_bash_timeout_kills_the_process_group(box):
    out = run(box, "bash", command="sleep 30 & sleep 30", timeout_s=1)
    assert "timed out after 1s" in out


def test_bash_environment_is_scrubbed(box, monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "hunter2")
    out = asyncio.run(box._t_bash("echo token=[$SUPER_SECRET_TOKEN] id=[$RAIDEN_INCIDENT_ID]"))
    assert "token=[]" in out and "id=[inc-t]" in out


def test_denied_calls_are_counted_audited_and_not_executed(box, cfg):
    marker = cfg.state_dir / "marker"
    out = run(box, "bash", command=f"pkill -f nothing; touch {marker}")
    assert out.startswith("DENIED by guardrail [no-mass-kill]")
    assert not marker.exists()
    assert box.denied == 1 and box.calls == 1
    audit = [json.loads(x) for x in cfg.audit_path.read_text().splitlines()]
    assert audit[-1]["decision"] == "deny"
    assert any(json.loads(x)["type"] == "guard_denied" for x in cfg.events_path.read_text().splitlines())


def test_load_skill(box, cfg):
    d = cfg.skills_dir / "demo"
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nbody")
    (d / "scripts" / "go.sh").write_text("echo")
    out = run(box, "load_skill", name="demo")
    assert "body" in out and "go.sh" in out and "reference files" not in out
    (d / "references").mkdir()
    (d / "references" / "notes.md").write_text("# ref")
    assert str(d / "references" / "notes.md") in run(box, "load_skill", name="demo")
    assert "Available: demo" in run(box, "load_skill", name="nope")
    assert "invalid skill name" in run(box, "load_skill", name="../etc")


def test_escalate_and_finish(box, cfg):
    assert "escalation recorded" in run(box, "escalate", summary="cannot fix", details="x")
    assert (cfg.escalations_dir / "inc-t.md").read_text().startswith("# Escalation")
    run(box, "finish", resolution="escalated", summary="done")
    assert box.finished == ("escalated", "done")


def test_bad_arguments_are_data_not_crashes(box):
    assert "bad arguments" in run(box, "read_file", nonsense=1)
    assert run(box, "read_file", path="/nonexistent/file").startswith("ERROR: FileNotFoundError")


def test_clip_keeps_head_and_tail():
    text = "A" * 5000 + "B" * 20000 + "C" * 5000
    out = clip(text, 12_000)
    assert out.startswith("A") and out.endswith("C") and "characters elided" in out and len(out) < 12_200


def test_notes_with_quoted_variable_paths_and_redirects(box, cfg):
    cmd = f'RAIDEN_INCIDENT_DIR={box.incident_dir}; printf "%s note\\n" "$(date -u +%FT%TZ)" >> "$RAIDEN_INCIDENT_DIR/notes.md"; cat "$RAIDEN_INCIDENT_DIR/notes.md" 2>&1'
    out = run(box, "bash", command=cmd)
    assert not out.startswith("DENIED"), out
    assert "note" in (box.incident_dir / "notes.md").read_text() and box.denied == 0
    out = run(box, "bash", command="printf '%s\\n' 'Added `/opt/raiden/checks.d/65-x.sh`; owner is '\\''arena'\\'' now' >> $RAIDEN_INCIDENT_DIR/notes.md")
    assert not out.startswith("DENIED"), out
