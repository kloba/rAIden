"""Skill index, lessons memory and artifact validation for the evolve step."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import yaml

from .config import Config
from .events import utcnow_iso


def skill_index(cfg: Config) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not cfg.skills_dir.is_dir():
        return out
    for md in sorted(cfg.skills_dir.glob("*/SKILL.md")):
        meta = parse_frontmatter(md.read_text(errors="replace"))
        if meta and meta.get("name") == md.parent.name:
            out.append((meta["name"], " ".join(str(meta.get("description", "")).split())))
    return out


FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


def _yaml_frontmatter(text: str) -> dict | None:
    m = FM.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _loose_frontmatter(text: str) -> dict | None:
    """name/description recovered line by line (models like to write `description: ... statement: DROP ...`)."""
    m = FM.match(text)
    if not m:
        return None
    name = re.search(r"^name:[ \t]*(.+?)[ \t]*$", m.group(1), re.M)
    desc = re.search(r"^description:[ \t]*(.*?)(?=^\S|\Z)", m.group(1), re.M | re.S)
    if not name or not desc:
        return None
    text_ = " ".join(desc.group(1).split()).strip()
    text_ = re.sub(r"^[>|][-+]?\s*", "", text_).strip().strip("\"'")
    return {"name": name.group(1).strip().strip("\"'"), "description": text_} if text_ else None


def parse_frontmatter(text: str) -> dict | None:
    return _yaml_frontmatter(text) or _loose_frontmatter(text)


def repair_frontmatter(text: str) -> str | None:
    """If the frontmatter is not valid YAML but recoverable, return the text with a properly quoted description."""
    if _yaml_frontmatter(text) is not None:
        return None
    meta = _loose_frontmatter(text)
    m = FM.match(text)
    if not meta or not m:
        return None
    block = re.sub(r"^description:[ \t]*.*?(?=^\S|\Z)", lambda _m: "description: " + json.dumps(meta["description"]) + "\n", m.group(1), count=1, flags=re.M | re.S)
    return "---\n" + block.rstrip("\n") + "\n---\n" + text[m.end():]


def read_lessons(cfg: Config, keywords: set[str], max_chars: int = 3500) -> str:
    """Return the lessons most relevant to the current signatures (keyword overlap), newest last."""
    try:
        lines = [ln for ln in cfg.lessons_path.read_text().splitlines() if ln.strip().startswith("- ")]
    except FileNotFoundError:
        return ""
    scored = []
    for i, ln in enumerate(lines):
        low = ln.lower()
        hits = sum(1 for k in keywords if k and k in low)
        scored.append((hits, i, ln))
    chosen = sorted(scored, key=lambda t: (-t[0], -t[1]))[:14]
    chosen.sort(key=lambda t: t[1])
    text = "\n".join(ln for _, _, ln in chosen)
    return text[-max_chars:]


def recent_incidents(cfg: Config, n: int = 5) -> str:
    rows = []
    try:
        dirs = sorted(d for d in cfg.incidents_dir.iterdir() if (d / "incident.json").is_file())[-n:]
    except FileNotFoundError:
        return ""
    for d in dirs:
        try:
            m = json.loads((d / "incident.json").read_text())
        except Exception:
            continue
        rows.append(f"- {m.get('id')} [{m.get('outcome', 'open')}] {m.get('reason', '')[:100]} | {m.get('summary', '')[:140]}")
    return "\n".join(rows)


# ---------------------------------------------------------------------------- evolve validation
def snapshot(cfg: Config) -> dict[str, str]:
    snap: dict[str, str] = {}
    for root in (cfg.skills_dir, cfg.checks_dir):
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                snap[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _check_script(path: Path) -> str | None:
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"bash -n failed: {r.stderr.strip()[:200]}"
    if path.parent.name == "checks.d":
        path.chmod(0o755)
        try:
            r = subprocess.run([str(path)], capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "check did not finish within 30 s"
        last = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode not in (0, 1, 2) or not re.match(r"^(OK|WARN|CRIT)\b", last):
            return f"check must print 'OK|WARN|CRIT <name>: ...' last and exit 0/1/2 (exit={r.returncode}, last line={last[:80]!r})"
    return None


def validate_changes(cfg: Config, before: dict[str, str]) -> list[dict]:
    """Compare against the snapshot taken before the evolve session; quarantine anything invalid."""
    after = snapshot(cfg)
    changes: list[dict] = []
    for path_s, digest in after.items():
        if before.get(path_s) == digest:
            continue
        action = "modified" if path_s in before else "added"
        p = Path(path_s)
        problem: str | None = None
        if p.name == "SKILL.md":
            body = p.read_text(errors="replace")
            meta = parse_frontmatter(body)
            if not meta or meta.get("name") != p.parent.name or not str(meta.get("description", "")).strip():
                problem = "SKILL.md needs YAML frontmatter with name == directory name and a description"
            else:
                fixed = repair_frontmatter(body)
                if fixed is not None:
                    p.write_text(fixed)
                    changes.append({"path": path_s, "action": action, "note": "frontmatter repaired (description quoted)"})
                    continue
        elif p.suffix == ".sh":
            problem = _check_script(p)
        if problem:
            qdir = cfg.quarantine_dir / utcnow_iso().replace(":", "")
            qdir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(qdir / p.name))
            if action == "modified":
                pass  # the agent can edit again; the original is not restored automatically
            changes.append({"path": path_s, "action": "quarantined", "reason": problem})
        else:
            changes.append({"path": path_s, "action": action})
    return changes
