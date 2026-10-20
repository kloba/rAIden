import os
import stat

from raiden.incident import Incident
from raiden.memory import parse_frontmatter, read_lessons, recent_incidents, repair_frontmatter, skill_index, snapshot, validate_changes
from raiden.patrol import patrol_incident, run_checks, worst


def write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)


def test_skill_index_requires_matching_frontmatter(cfg):
    write(cfg.skills_dir / "good" / "SKILL.md", "---\nname: good\ndescription: >\n  multi\n  line\n---\nbody\n")
    write(cfg.skills_dir / "wrongname" / "SKILL.md", "---\nname: other\ndescription: x\n---\n")
    write(cfg.skills_dir / "nofm" / "SKILL.md", "just text")
    assert skill_index(cfg) == [("good", "multi line")]
    assert parse_frontmatter("---\n: : :\n---\n") is None


def test_lessons_prefer_relevant_lines(cfg):
    cfg.memory_dir.mkdir(parents=True)
    cfg.lessons_path.write_text("\n".join([f"- unrelated lesson {i}" for i in range(30)] + ["- pg_crash lives in conf.d/50-experiment.conf"]) + "\n")
    out = read_lessons(cfg, {"crash", "pg_crash"})
    assert "pg_crash lives in conf.d" in out and len(out.splitlines()) <= 14


def test_recent_incidents(cfg):
    for i in range(7):
        d = cfg.incidents_dir / f"inc-{i}"
        d.mkdir(parents=True)
        (d / "incident.json").write_text(f'{{"id": "inc-{i}", "outcome": "resolved", "reason": "r{i}", "summary": "s{i}"}}')
    out = recent_incidents(cfg, 3).splitlines()
    assert len(out) == 3 and out[-1].startswith("- inc-6")


def test_validate_changes_quarantines_invalid_artifacts(cfg):
    write(cfg.skills_dir / "existing" / "SKILL.md", "---\nname: existing\ndescription: x\n---\n")
    before = snapshot(cfg)
    write(cfg.skills_dir / "learned-ok" / "SKILL.md", "---\nname: learned-ok\ndescription: fine\n---\n")
    write(cfg.skills_dir / "learned-bad" / "SKILL.md", "---\nname: wrong\ndescription: x\n---\n")
    write(cfg.checks_dir / "91-ok.sh", "#!/bin/bash\necho 'WARN demo: something'\nexit 1\n")
    write(cfg.checks_dir / "92-nolast.sh", "#!/bin/bash\necho hello\n")
    write(cfg.checks_dir / "93-syntax.sh", "#!/bin/bash\nif then fi (\n")
    changes = {c["path"].split("/")[-1] if c["path"].endswith(".sh") else c["path"].split("/")[-2]: c for c in validate_changes(cfg, before)}
    assert changes["learned-ok"]["action"] == "added"
    assert changes["91-ok.sh"]["action"] == "added"
    for name in ("learned-bad", "92-nolast.sh", "93-syntax.sh"):
        assert changes[name]["action"] == "quarantined" and changes[name]["reason"], name
    assert os.access(cfg.checks_dir / "91-ok.sh", os.X_OK)
    assert not (cfg.checks_dir / "92-nolast.sh").exists()


def test_patrol_parses_verdicts(cfg):
    write(cfg.checks_dir / "10-a.sh", "#!/bin/bash\necho noise\necho 'OK alpha: all good'\n", 0o755)
    write(cfg.checks_dir / "20-b.sh", "#!/bin/bash\necho 'WARN beta: backup is 30h old'\nexit 1\n", 0o755)
    write(cfg.checks_dir / "30-c.sh", "#!/bin/bash\necho 'CRIT gamma: archiver failing'\nexit 2\n", 0o755)
    write(cfg.checks_dir / "40-d.sh", "#!/bin/bash\necho gibberish\n", 0o755)
    res = run_checks(cfg)
    assert [(r["name"], r["status"]) for r in res] == [("alpha", "OK"), ("beta", "WARN"), ("gamma", "CRIT"), ("40-d", "CRIT")]
    assert worst(res) == "CRIT" and worst(res[:2]) == "WARN" and worst(res[:1]) == "OK"
    inc = patrol_incident(res)
    assert isinstance(inc, Incident) and inc.critical and set(inc.signatures) == {"patrol:beta", "patrol:gamma", "patrol:40-d"}
    assert patrol_incident(res[:1]) is None
    assert "backup is 30h old" in inc.prompt_block()


def test_patrol_timeout_is_a_failure_not_a_hang(cfg):
    write(cfg.checks_dir / "10-slow.sh", "#!/bin/bash\nsleep 5\necho 'OK slow: x'\n", 0o755)
    res = run_checks(cfg, timeout_s=1)
    assert res[0]["status"] == "CRIT" and "timed out" in res[0]["message"]


def test_frontmatter_with_unquoted_colon_is_repaired_not_quarantined(cfg):
    before = snapshot(cfg)
    body = "---\nname: learned-dropped\ndescription: repeated 42P01 after LOG statement: DROP TABLE x - restore from PITR\n---\n# body\n"
    write(cfg.skills_dir / "learned-dropped" / "SKILL.md", body)
    changes = validate_changes(cfg, before)
    assert [c["action"] for c in changes] == ["added"] and "repaired" in changes[0]["note"]
    fixed = (cfg.skills_dir / "learned-dropped" / "SKILL.md").read_text()
    assert parse_frontmatter(fixed)["description"].endswith("restore from PITR") and fixed.endswith("# body\n")
    import yaml

    assert yaml.safe_load(fixed.split("---")[1])["name"] == "learned-dropped"  # valid YAML now
    assert skill_index(cfg) == [("learned-dropped", "repeated 42P01 after LOG statement: DROP TABLE x - restore from PITR")]
    assert repair_frontmatter(fixed) is None  # already valid: untouched


def test_unrecoverable_frontmatter_is_still_quarantined(cfg):
    before = snapshot(cfg)
    write(cfg.skills_dir / "learned-nodesc" / "SKILL.md", "---\nname: learned-nodesc\n---\nbody")
    write(cfg.skills_dir / "learned-other" / "SKILL.md", "---\nname: something-else\ndescription: fine: yes\n---\nbody")
    actions = {c["path"].split("/")[-2]: c["action"] for c in validate_changes(cfg, before)}
    assert actions == {"learned-nodesc": "quarantined", "learned-other": "quarantined"}
