"""The agent's tools. Every call goes through Guard.check() and is audited; denials are returned to the model."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
from pathlib import Path
from typing import Any

from .config import Config
from .events import AuditLog, EventLog, utcnow_iso
from .guardrails import Guard
from .incident import PROCESS_DEATH_RE

MAX_OUT = 12_000

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command on the database host as the unprivileged 'raiden' OS user (bash -c). "
                "Use absolute paths. 'sudo -n -u postgres ...' and 'sudo -n systemctl <verb> postgresql@18-main' are allowed. "
                "Output is truncated; each call has a timeout (default 120 s, max 600 s). Guardrails may DENY a command: "
                "the denial text is returned instead of output."
            ),
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer", "minimum": 1, "maximum": 600}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file (numbered lines). Use offset/limit for big files.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file. Allowed areas: /var/lib/raiden/** (notes, reports, memory, work), /opt/raiden/.claude/skills/**, /opt/raiden/checks.d/**.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact string in a file you are allowed to write (must be unique unless replace_all).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "replace_all": {"type": "boolean"}},
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "Load a runbook (skill) by name; returns its SKILL.md and lists its helper scripts. See the skill index in the system prompt.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pg_probe",
            "description": "Fast structured health probe of PostgreSQL (readiness, recovery state, uptime, sessions, background workers, preload libraries, restart budget left). Includes the systemd state of the service (a unit stuck 'deactivating' means a hung shutdown). Safe to call repeatedly, also while the database flaps.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Hand the problem to a human when it cannot be fixed safely. Records an escalation note; then call finish with resolution 'escalated'.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "details": {"type": "string"}}, "required": ["summary"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "End the session. Call it exactly once, after verification, the report and the lessons are written.",
            "parameters": {
                "type": "object",
                "properties": {"resolution": {"type": "string", "enum": ["resolved", "mitigated", "escalated"]}, "summary": {"type": "string"}},
                "required": ["resolution", "summary"],
            },
        },
    },
]

PROBE_SQL = """
SELECT json_build_object(
  'ready', true,
  'in_recovery', pg_is_in_recovery(),
  'postmaster_start', pg_postmaster_start_time(),
  'uptime_s', extract(epoch FROM now() - pg_postmaster_start_time())::int,
  'client_sessions', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'),
  'max_connections', current_setting('max_connections')::int,
  'backend_types', (SELECT json_object_agg(backend_type, n) FROM (SELECT backend_type, count(*) n FROM pg_stat_activity GROUP BY 1) s),
  'shared_preload_libraries', current_setting('shared_preload_libraries'),
  'oldest_xact_s', (SELECT coalesce(max(extract(epoch FROM now() - xact_start))::int, 0) FROM pg_stat_activity WHERE xact_start IS NOT NULL),
  'checksum_failures', (SELECT coalesce(sum(checksum_failures), 0) FROM pg_stat_database)
)::text
"""


def clip(text: str, limit: int = MAX_OUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit // 3, limit - limit // 3
    return f"{text[:head]}\n...[{len(text) - limit} characters elided]...\n{text[-tail:]}"


class ToolBox:
    def __init__(self, cfg: Config, guard: Guard, audit: AuditLog, events: EventLog, incident_id: str, incident_dir: Path) -> None:
        self.cfg, self.guard, self.audit, self.events = cfg, guard, audit, events
        self.incident_id, self.incident_dir = incident_id, incident_dir
        self.finished: tuple[str, str] | None = None
        self.calls = 0
        self.denied = 0

    # ------------------------------------------------------------------ dispatch
    @staticmethod
    def _summary(name: str, args: dict) -> str:
        if name == "bash":
            return " ".join(str(args.get("command", "")).split())[:300]
        if name in ("read_file", "write_file", "edit_file"):
            return str(args.get("path", ""))
        if name == "load_skill":
            return str(args.get("name", ""))
        if name in ("finish", "escalate"):
            return str(args.get("summary", ""))[:200]
        return ""

    def _expand(self, args: dict) -> dict:
        """Substitute the two incident variables textually so the guard sees literal paths."""
        values = {"RAIDEN_INCIDENT_DIR": str(self.incident_dir), "RAIDEN_INCIDENT_ID": self.incident_id}
        out = dict(args)
        for key in ("command", "path"):
            if isinstance(out.get(key), str):
                for var, val in values.items():
                    out[key] = re.sub(r"\$\{%s\}|\$%s\b" % (var, var), lambda _m, v=val: v, out[key])
        return out

    async def call(self, name: str, args: dict) -> str:
        self.calls += 1
        args = self._expand(args)
        summary = self._summary(name, args)
        decision = self.guard.check(name, args)
        if not decision.allow:
            self.denied += 1
            self.audit.record(self.incident_id, name, summary, "deny", f"{decision.rule}: {decision.reason}")
            self.events.emit("guard_denied", incident=self.incident_id, tool=name, rule=decision.rule, summary=summary[:200])
            return f"DENIED by guardrail [{decision.rule}]: {decision.reason}"
        self.audit.record(self.incident_id, name, summary, "allow")
        try:
            handler = getattr(self, f"_t_{name}")
            return clip(await handler(**args))
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except Exception as exc:  # tool failures are data for the model, not crashes
            return f"ERROR: {exc.__class__.__name__}: {exc}"

    # ------------------------------------------------------------------ tools
    def _env(self) -> dict[str, str]:
        return {
            "PATH": self.cfg.path_env,
            "HOME": str(self.cfg.state_dir),
            "USER": "raiden",
            "LOGNAME": "raiden",
            "LANG": "C.UTF-8",
            "PGCONNECT_TIMEOUT": "3",
            "RAIDEN_INCIDENT_ID": self.incident_id,
            "RAIDEN_INCIDENT_DIR": str(self.incident_dir),
        }

    async def _t_bash(self, command: str, timeout_s: int = 120) -> str:
        timeout_s = max(1, min(int(timeout_s), 600))
        with tempfile.TemporaryFile() as out:
            proc = await asyncio.create_subprocess_exec(
                "/bin/bash", "-c", command, stdout=out, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, cwd=str(self.cfg.workdir), env=self._env(), start_new_session=True,
            )
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            out.seek(0)
            text = out.read().decode("utf-8", "replace")
        tail = f"\n[timed out after {timeout_s}s and was killed]" if timed_out else f"\n[exit {proc.returncode}]"
        return clip(text.rstrip("\n")) + tail

    async def _t_read_file(self, path: str, offset: int = 1, limit: int = 300) -> str:
        p = Path(self.guard._norm(path))
        lines = p.read_text(errors="replace").splitlines()
        offset = max(1, int(offset))
        chunk = lines[offset - 1 : offset - 1 + int(limit)]
        body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, start=offset))
        more = f"\n[{len(lines) - (offset - 1 + len(chunk))} more lines]" if offset - 1 + len(chunk) < len(lines) else ""
        return body + more

    async def _t_write_file(self, path: str, content: str) -> str:
        p = Path(self.guard._norm(path))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        p.chmod(0o755 if p.suffix == ".sh" else 0o644)
        return f"wrote {len(content)} bytes to {p}"

    async def _t_edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = Path(self.guard._norm(path))
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return "ERROR: the old string was not found"
        if count > 1 and not replace_all:
            return f"ERROR: the old string occurs {count} times; add context or set replace_all"
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return f"edited {p} ({count if replace_all else 1} replacement)"

    async def _t_load_skill(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            return "ERROR: invalid skill name"
        d = self.cfg.skills_dir / name
        md = d / "SKILL.md"
        if not md.is_file():
            names = sorted(p.name for p in self.cfg.skills_dir.iterdir() if (p / "SKILL.md").is_file())
            return f"ERROR: no skill named {name!r}. Available: {', '.join(names)}"
        text = md.read_text()
        scripts = sorted(str(p) for p in (d / "scripts").glob("*")) if (d / "scripts").is_dir() else []
        return text + ("\n\n[helper scripts]\n" + "\n".join(scripts) if scripts else "")

    async def _t_pg_probe(self) -> str:
        env = {**self._env(), "PGCONNECT_TIMEOUT": "3"}
        proc = await asyncio.create_subprocess_exec(
            "psql", "-X", "-At", "-d", "postgres", "-c", PROBE_SQL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 15)
        except asyncio.TimeoutError:
            proc.kill()
            return json.dumps({"ready": False, "error": "probe timed out"})
        unit, unit_age = self.guard._unit_state()
        extra = {"restarts_left": self.guard.restarts_left(), "crash_lines_last_300kb": self._crash_lines(), "service_state": unit, "service_state_for_s": int(unit_age)}
        if proc.returncode == 0:
            data = json.loads(stdout.decode().strip() or "{}")
            return json.dumps({**data, **extra})
        ready = await asyncio.create_subprocess_exec("pg_isready", "-h", "/var/run/postgresql", stdout=asyncio.subprocess.PIPE, env=env)
        out, _ = await ready.communicate()
        return json.dumps({"ready": False, "pg_isready": out.decode().strip(), "error": stderr.decode().strip()[:300], **extra})

    def _crash_lines(self) -> int:
        try:
            files = sorted(self.cfg.pg_log_dir.glob(self.cfg.pg_log_glob))
            if not files:
                return 0
            with open(files[-1], "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 300_000))
                data = fh.read().decode("utf-8", "replace")
            return sum(1 for line in data.splitlines() if PROCESS_DEATH_RE.search(line))
        except Exception:
            return -1

    async def _t_escalate(self, summary: str, details: str = "") -> str:
        self.cfg.escalations_dir.mkdir(parents=True, exist_ok=True)
        path = self.cfg.escalations_dir / f"{self.incident_id}.md"
        path.write_text(f"# Escalation for {self.incident_id}\n\n{summary}\n\n{details}\n")
        self.events.emit("escalation", incident=self.incident_id, summary=summary[:300])
        return f"escalation recorded at {path}; now call finish with resolution 'escalated'"

    async def _t_finish(self, resolution: str, summary: str) -> str:
        self.finished = (resolution, summary)
        return "recorded; the session ends now"
