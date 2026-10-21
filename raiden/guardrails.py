"""Guardrails: every tool call of the agent passes through Guard.check() BEFORE it runs.

Defence in depth (the model is cooperative, but honest mistakes are how databases die):
  1. OS privileges: the agent is a normal user with a narrow sudoers file;
  2. this policy engine (deny rules, path confinement, a restart budget);
  3. per-incident turn / time limits and a full audit trail.

Rules about WHAT a command does are evaluated on parsed tokens (after wrappers such as sudo/env/timeout are
stripped), so quoting tricks and multi-line scripts do not hide a `pkill`. Rules about CONTENT (secrets,
durability settings, COPY PROGRAM) are regexes over the whole text.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    rule: str = ""


ALLOW = Decision(True)


def deny(rule: str, reason: str) -> Decision:
    return Decision(False, reason, rule)


# ---- content rules: matched against the whole text (also heredocs); script scans skip no-secrets --------
DENY_ANY: list[tuple[str, re.Pattern[str], str]] = [
    ("no-secrets", re.compile(r"/proc/[^\s]*/environ|169\.254\.169\.254|/etc/raiden|/\.azure(?:/|\b)|IDENTITY_(?:ENDPOINT|HEADER)|arena-private"), "reading credentials or the environment is not allowed"),
    ("no-weaken-durability", re.compile(r"\b(fsync|full_page_writes|synchronous_commit)\s*(?:=|\s+to\s+|\s)\s*['\"]?(?:off|false|0|no|local|remote_write)\b", re.I), "never weaken durability settings"),
    ("no-pg-program", re.compile(r"\b(?:to|from)\s+program\b", re.I), "COPY ... PROGRAM executes shell commands"),
    ("no-self-harm", re.compile(r"\b(?:alter|drop)\s+(?:role|user)\s+\"?raiden\b", re.I), "do not touch the agent's own database role"),
    ("no-psql-shell", re.compile(r"(?:^|[\s\"';])\\!"), "psql's \\! runs shell commands outside the guardrails"),
    ("protection-tamper", re.compile(
        r"\b(?:drop|alter)\s+event\s+trigger\s+(?:if\s+exists\s+)?\"?raiden_|\bevent_triggers\s*(?:=|\s+to)\s*['\"]?(?:off|false|0)\b|"
        r"\ballow_alter_system\s*(?:=|\s+to|\s)\s*['\"]?(?:on|true|1)\b", re.I),
     "protections that rAIden installed (event triggers, allow_alter_system = off) may not be removed or switched off by rAIden; escalate if one blocks legitimate work"),
]

# ---- program rules: matched against the command word (basename) -------------------------------------------
DENY_PROGS: dict[str, tuple[str, str]] = {}


def _progs(rule: str, reason: str, *names: str) -> None:
    for n in names:
        DENY_PROGS[n] = (rule, reason)


_progs("no-resetwal", "pg_resetwal destroys WAL history; escalate to a human instead", "pg_resetwal", "pg_resetxlog")
_progs("no-poweroff", "never power off or reboot the host", "shutdown", "reboot", "halt", "poweroff")
_progs("no-mass-kill", "no pkill/killall: it can take down the postmaster; use pg_terminate_backend or a targeted kill", "pkill", "killall")
_progs("no-network", "network access and package installs are not allowed during incidents",
       "curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "sftp", "ftp", "telnet", "pip", "pip3", "pipx", "apt", "apt-get", "dpkg", "snap", "npm")
_progs("no-secrets", "reading credentials or the environment is not allowed", "printenv")
_progs("no-persistence", "no persistence or account changes", "crontab", "systemd-run", "useradd", "usermod", "userdel", "passwd", "chpasswd", "visudo", "at")
_progs("no-system-admin", "host administration is out of scope",
       "iptables", "ip6tables", "nft", "ufw", "fdisk", "parted", "losetup", "modprobe", "insmod", "docker", "podman", "mount", "umount")
_progs("no-su", "no su/runuser", "su", "runuser")
_progs("sql-drop", "dropping databases/clusters/roles is not allowed", "dropdb", "dropuser", "pg_dropcluster")

INTERPRETER = re.compile(r"python[0-9.]*|perl|ruby|node|nodejs|php|lua")
SAFE_TABLE = re.compile(
    r"^(?:if\s+exists\s+)?(?:\"?\w+\"?\.)?\"?(?:tmp|temp|scratch|restore|restored|quarantine|pitr)_\w*\"?(?:\s|;|$)|pg_temp",
    re.I,
)
CLONE_HINT = re.compile(r"/var/lib/raiden/work/|(?:-p|--port)[ =]5433\b|PGPORT=5433")

DELETERS = {"rm", "rmdir", "unlink", "shred"}
COMPRESSORS = {"gzip", "xz", "bzip2", "zstd"}
PATH_WRITERS = {"chmod", "chown", "touch", "mkdir", "truncate"}
SUDO_NON_PG = {"systemctl", "journalctl", "dmesg"}
WRAPPERS = {"nohup", "time", "nice", "ionice", "command", "exec", "stdbuf", "setsid"}
KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}
XARGS_VALUE_FLAGS = {"-n", "-P", "-I", "-L", "-s", "-d", "-E", "-a", "-i"}
XARGS_UNVERIFIABLE = DELETERS | COMPRESSORS | PATH_WRITERS | {"mv", "cp", "tee", "dd", "install", "ln", "rsync", "kill", "sed", "bash", "sh", "dash", "eval", "find", "xargs"}
FIND_RISKY = {"-delete", "-exec", "-execdir", "-ok", "-okdir"}


class Guard:
    def __init__(self, cfg: Config, now: Callable[[], float] = time.time, unit_state: Callable[[], tuple[str, float]] | None = None) -> None:
        self.cfg = cfg
        self.now = now
        self._unit_state = unit_state or self._systemd_unit_state
        self.write_roots = [
            str(cfg.state_dir),
            str(cfg.skills_dir),
            str(cfg.checks_dir),
            "/tmp/raiden-",
            str(cfg.pg_conf_dir / "conf.d"),
        ]
        self.delete_roots = [str(cfg.work_dir), "/tmp/raiden-"]
        self.protected = {str(cfg.events_path), str(cfg.audit_path), str(cfg.restarts_path)}
        self.read_deny = [
            re.compile(p)
            for p in (r"^/etc/raiden", r"^/var/lib/arena-private", r"^/proc/[^/]+/environ", r"/\.azure(/|$)", r"^/var/lib/raiden/\.")
        ]

    # ------------------------------------------------------------------ entry point
    def check(self, tool: str, args: dict) -> Decision:
        try:
            if tool == "bash":
                return self.check_bash(str(args.get("command", "")))
            if tool == "write_file":
                return self.check_write_tool(str(args.get("path", "")), str(args.get("content", "")))
            if tool == "edit_file":
                return self.check_write_tool(str(args.get("path", "")), str(args.get("new", "")))
            if tool == "read_file":
                return self.check_read(str(args.get("path", "")))
            if tool in ("load_skill", "pg_probe", "escalate", "finish"):
                return ALLOW
            return deny("unknown-tool", f"tool {tool!r} is not available")
        except Exception as exc:  # a guard that crashes must fail closed
            return deny("guard-error", f"could not safely evaluate this call ({exc.__class__.__name__}: {exc}); simplify it")

    # ------------------------------------------------------------------ paths
    def _norm(self, path: str) -> str:
        if path.startswith("~"):
            path = str(self.cfg.state_dir) + path[1:]
        return os.path.normpath(os.path.join(str(self.cfg.workdir), path))

    @staticmethod
    def _within(path: str, roots: list[str]) -> bool:
        for r in roots:
            if r.endswith("-"):  # name-prefix root such as /tmp/raiden-
                if path.startswith(r):
                    return True
            elif path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/"):
                return True
        return False

    def path_write_allowed(self, raw: str, delete: bool = False, extra_roots: list[str] | None = None) -> Decision:
        if not raw:
            return deny("path-empty", "empty path")
        if ".." in raw.replace("\\", "/").split("/"):
            return deny("path-dotdot", f"'..' in {raw!r}")
        cuts = [raw.find(c) for c in "$`*?[" if raw.find(c) != -1]
        cut = min(cuts) if cuts else len(raw)
        dynamic = cut < len(raw)
        literal = raw[:cut]
        has_var = "$" in raw or "`" in raw
        if dynamic and (not literal or (has_var and literal.endswith("/"))):
            return deny("path-unresolved", f"cannot verify {raw!r}; use a literal path (a name prefix before $VAR is fine)")
        path = self._norm(literal)
        if path in self.protected:
            return deny("path-protected", f"{path} is an append-only audit file")
        roots = list(self.delete_roots if delete else self.write_roots) + (extra_roots or [])
        if not self._within(path, roots):
            return deny("path-confinement", f"{raw!r} is outside the {'deletable' if delete else 'writable'} areas: {', '.join(roots)}")
        is_root = any(path == r.rstrip("/") for r in roots if not r.endswith("-"))
        children_glob = dynamic and not has_var and literal.endswith("/")
        if is_root and delete and not children_glob:
            return deny("path-root", f"cannot delete the area {path} itself")
        return ALLOW

    def check_write_tool(self, path: str, content: str) -> Decision:
        d = self.path_write_allowed(path)
        if not d.allow:
            return d
        norm = self._norm(path)
        if norm.endswith(".sh") or "/scripts/" in norm or norm.startswith(str(self.cfg.checks_dir)):
            return self.scan_script(content)
        if norm.endswith(".sql"):
            d = self._check_sql(content, bool(CLONE_HINT.search(content)))
            return d if not d.allow else ALLOW
        return ALLOW

    def check_read(self, path: str) -> Decision:
        norm = self._norm(path)
        for rx in self.read_deny:
            if rx.search(norm):
                return deny("read-secret", f"reading {norm} is not allowed")
        return ALLOW

    # ------------------------------------------------------------------ scripts (written now, run later)
    def scan_script(self, content: str) -> Decision:
        for rule, rx, reason in DENY_ANY:
            if rule != "no-secrets" and rx.search(content):
                return deny("script-content:" + rule, f"script content violates policy: {reason}")
        for raw, _ in self._split(content):
            try:
                toks = shlex.split(self._blank_subst(raw), posix=True)
            except ValueError:
                continue
            argv, _, _ = self._strip(toks)
            if argv:
                hit = self._deny_prog(os.path.basename(argv[0]), argv[1:])
                if hit:
                    return deny("script-content:" + hit.rule, f"script content violates policy: {hit.reason}")
        d = self._check_sql(self._sql_text(content), bool(CLONE_HINT.search(content)))
        return d if not d.allow else ALLOW

    # ------------------------------------------------------------------ command substitutions
    @staticmethod
    def _balanced_end(text: str, start: int) -> int:
        """Index just past the ')' that closes the '$(' at `start` (or len(text) if unbalanced)."""
        depth, j, n = 1, start + 2, len(text)
        while j < n and depth:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        return j

    @classmethod
    def _subst_spans(cls, text: str) -> list[tuple[int, int, str, str]]:
        """(start, end, body, kind) of every $(...), `...` and $((...)) the shell would really evaluate:
        inside single quotes they are literal text (markdown code spans in a note must not look like commands)."""
        spans: list[tuple[int, int, str, str]] = []
        i, n = 0, len(text)
        single = double = False
        while i < n:
            c = text[i]
            if single:
                single = c != "'"
                i += 1
            elif c == "\\" and i + 1 < n:
                i += 2
            elif c == "'" and not double:
                single = True
                i += 1
            elif c == '"':
                double = not double
                i += 1
            elif text.startswith("$((", i):
                j = text.find("))", i)
                e = n if j == -1 else j + 2
                spans.append((i, e, "", "arith"))
                i = e
            elif text.startswith("$(", i):
                e = cls._balanced_end(text, i)
                spans.append((i, e, text[i + 2 : e - 1] if text[e - 1 : e] == ")" else text[i + 2 :], "sub"))
                i = e
            elif c == "`":
                j = text.find("`", i + 1)
                e = n if j == -1 else j + 1
                spans.append((i, e, text[i + 1 : (j if j != -1 else n)], "sub"))
                i = e
            else:
                i += 1
        return spans

    @classmethod
    def _substitutions(cls, text: str) -> list[str]:
        """Bodies of the outermost substitutions (nested ones are found when the body itself is checked)."""
        return [body for _, _, body, kind in cls._subst_spans(text) if kind == "sub"]

    @classmethod
    def _blank_subst(cls, text: str) -> str:
        """Replace every substitution by a placeholder so that whitespace inside it cannot split a token."""
        out = text
        for start, end, _, kind in reversed(cls._subst_spans(text)):
            out = out[:start] + ("$ARITH" if kind == "arith" else "$SUBST") + out[end:]
        return out

    # ------------------------------------------------------------------ what counts as executable SQL
    @staticmethod
    def _heredocs(cmd: str) -> list[tuple[str, str, bool]]:
        """(header line, body, literal) of every heredoc; a quoted delimiter (<<'EOF') makes the body literal text."""
        out: list[tuple[str, str, bool]] = []
        lines = cmd.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            i += 1
            m = re.search(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2", line)
            if m and "<<<" not in line:
                body: list[str] = []
                while i < len(lines):
                    cur = lines[i]
                    i += 1
                    if (cur.lstrip("\t") if m.group(1) else cur) == m.group(3):
                        break
                    body.append(cur)
                out.append((line, "\n".join(body), bool(m.group(2))))
        return out

    @classmethod
    def _without_literal_heredocs(cls, cmd: str) -> str:
        """The command text with the bodies of quoted-delimiter heredocs removed (nothing in them is evaluated)."""
        lines = cmd.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            out.append(line)
            i += 1
            m = re.search(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2", line)
            if m and "<<<" not in line:
                literal = bool(m.group(2))
                while i < len(lines):
                    cur = lines[i]
                    i += 1
                    if (cur.lstrip("\t") if m.group(1) else cur) == m.group(3):
                        break
                    if not literal:
                        out.append(cur)
        return "\n".join(out)

    def _read_sql_file(self, path: str) -> str:
        try:
            with open(self._norm(path), errors="replace") as fh:
                return fh.read(200_000)
        except OSError:
            return ""

    def _sql_text(self, cmd: str) -> str:
        """Only text that executes as SQL: psql -c/-f arguments, heredocs and pipes into psql. Prose in notes, jq
        filters or grep patterns that merely MENTION 'DROP TABLE' must not trip the SQL rules."""
        parts: list[str] = []
        for raw, _ in self._split(cmd):
            try:
                toks = shlex.split(self._blank_subst(raw), posix=True)
            except ValueError:
                continue
            argv, _, _ = self._strip(toks)
            if not argv or os.path.basename(argv[0]) != "psql":
                continue
            args = argv[1:]
            for i, a in enumerate(args):
                nxt = args[i + 1] if i + 1 < len(args) else ""
                if a in ("-c", "--command"):
                    parts.append(nxt)
                elif a.startswith("--command="):
                    parts.append(a.split("=", 1)[1])
                elif a.startswith("-c") and not a.startswith("--") and len(a) > 2:
                    parts.append(a[2:])
                elif a in ("-f", "--file"):
                    parts.append(self._read_sql_file(nxt))
                elif a.startswith("--file="):
                    parts.append(self._read_sql_file(a.split("=", 1)[1]))
                elif a == "<":
                    parts.append(self._read_sql_file(nxt))
                elif a.startswith("<") and not a.startswith("<<") and len(a) > 1:
                    parts.append(self._read_sql_file(a[1:]))
        for header, body, _ in self._heredocs(cmd):
            if re.search(r"\bpsql\b", header):
                parts.append(body)
        if re.search(r"\|\s*(?:sudo\s+(?:-\S+\s+(?:\S+\s+)?)*)?(?:\S*/)?psql\b", cmd):
            parts.append(cmd)  # echo "..." | psql: be conservative and scan everything
        return ";\n".join(parts)

    # ------------------------------------------------------------------ bash
    def check_bash(self, cmd: str) -> Decision:
        if not cmd.strip():
            return deny("empty", "empty command")
        if "\x00" in cmd:
            return deny("nul", "NUL byte in command")
        for rule, rx, reason in DENY_ANY:
            if rx.search(cmd):
                return deny(rule, reason)
        d = self._check_sql(self._sql_text(cmd), bool(CLONE_HINT.search(cmd)))
        if not d.allow:
            return d
        for sub in self._substitutions(self._without_literal_heredocs(cmd)):
            if sub.strip():
                d = self.check_bash(sub)
                if not d.allow:
                    return d
        for header, body, _ in self._heredocs(cmd):
            if re.search(r"\b(bash|sh|dash)\b", header) and "-c" not in header:
                d = self.check_bash(body)
                if not d.allow:
                    return d
        pairs = self._split(cmd)
        for _, targets in pairs:
            for target in targets:
                if target in ("/dev/null", "/dev/stderr", "/dev/stdout"):
                    continue
                d = self.path_write_allowed(target)
                if not d.allow:
                    return d
        for raw, _ in pairs:
            try:
                toks = shlex.split(self._blank_subst(raw), posix=True)
            except ValueError as exc:
                raise ValueError(f"unbalanced quotes near: {raw[:60]!r}") from exc
            if toks:
                d = self._check_tokens(toks)
                if not d.allow:
                    return d
        return ALLOW

    @staticmethod
    def _read_word(cmd: str, j: int) -> tuple[str, int]:
        """One shell word starting at j, quotes removed and substitutions replaced by $SUBST. Returns (word, end)."""
        out: list[str] = []
        n = len(cmd)
        while j < n:
            c = cmd[j]
            if c in " \t\n;&|<>()":
                break
            if c == "'":
                k = cmd.find("'", j + 1)
                k = n if k == -1 else k
                out.append(cmd[j + 1 : k])
                j = k + 1
            elif c == '"':
                k, buf = j + 1, []
                while k < n and cmd[k] != '"':
                    if cmd[k] == "\\" and k + 1 < n:
                        buf.append(cmd[k + 1])
                        k += 2
                    elif cmd.startswith("$(", k):
                        buf.append("$SUBST")
                        k = Guard._balanced_end(cmd, k)
                    else:
                        buf.append(cmd[k])
                        k += 1
                out.append("".join(buf))
                j = k + 1
            elif c == "\\" and j + 1 < n:
                out.append(cmd[j + 1])
                j += 2
            elif cmd.startswith("$(", j) and not cmd.startswith("$((", j):
                out.append("$SUBST")
                j = Guard._balanced_end(cmd, j)
            elif c == "`":
                k = cmd.find("`", j + 1)
                out.append("$SUBST")
                j = n if k == -1 else k + 1
            else:
                out.append(c)
                j += 1
        return "".join(out), j

    @staticmethod
    def _split(cmd: str) -> list[tuple[str, list[str]]]:
        """Split a shell line into simple commands: (raw, redirect targets). Redirections are REMOVED from `raw` and
        their targets collected with proper quote handling, so `rm -rf x 2>/dev/null` is judged as `rm -rf x`
        and `>> "$DIR/notes.md"` as the path it really writes. Heredoc bodies are skipped."""
        raws: list[str] = []
        tgts: list[list[str]] = []
        cur: list[str] = []
        curt: list[str] = []
        i, n = 0, len(cmd)
        quote = ""
        heredocs: list[tuple[str, bool]] = []

        def flush() -> None:
            nonlocal cur, curt
            raws.append("".join(cur))
            tgts.append(curt)
            cur, curt = [], []

        while i < n:
            c = cmd[i]
            if quote:
                if c == "\\" and quote == '"' and i + 1 < n:
                    cur.append(c + cmd[i + 1])
                    i += 1
                else:
                    cur.append(c)
                    if c == quote:
                        quote = ""
            elif c in "'\"":
                quote = c
                cur.append(c)
            elif c == "$" and cmd.startswith("$(", i) and not cmd.startswith("$((", i):
                j = Guard._balanced_end(cmd, i)
                cur.append(cmd[i:j])
                i = j - 1
            elif c == "`":
                j = cmd.find("`", i + 1)
                j = n - 1 if j == -1 else j
                cur.append(cmd[i : j + 1])
                i = j
            elif c == "\\" and i + 1 < n:
                cur.append(c + cmd[i + 1])
                i += 1
            elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
                m = re.match(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2", cmd[i:])
                if m:
                    heredocs.append((m.group(3), bool(m.group(1))))
                    i += m.end() - 1
                else:
                    cur.append(c)
            elif c == ">":
                joined = "".join(cur)
                fd = re.search(r"(?:^|(?<=\s))\d+$", joined)  # a file-descriptor number glued to the operator: 2>file
                if fd:
                    cur = [joined[: fd.start()]]
                j = i + 1
                if j < n and cmd[j] == ">":
                    j += 1
                if j < n and cmd[j] == "&":  # >&2, 2>&1: no file involved
                    k = j + 1
                    while k < n and (cmd[k].isdigit() or cmd[k] == "-"):
                        k += 1
                    i = k - 1
                elif j < n and cmd[j] == "(":  # >(process substitution)
                    i = j - 1
                else:
                    if j < n and cmd[j] == "|":
                        j += 1
                    k = j
                    while k < n and cmd[k] in " \t":
                        k += 1
                    word, e = Guard._read_word(cmd, k)
                    if word:
                        curt.append(word)
                    i = e - 1
            elif c == "\n":
                flush()
                while heredocs:
                    delim, strip = heredocs.pop(0)
                    j = i + 1
                    found = False
                    while j <= n:
                        end_ = cmd.find("\n", j)
                        end_ = n if end_ == -1 else end_
                        line = cmd[j:end_]
                        if (line.lstrip("\t") if strip else line) == delim:
                            i, found = end_, True
                            break
                        if end_ >= n:
                            break
                        j = end_ + 1
                    if not found:
                        i = n
            elif c in ";&|":
                flush()
                if i + 1 < n and cmd[i + 1] == c:
                    i += 1
            else:
                cur.append(c)
            i += 1
        flush()
        out: list[tuple[str, list[str]]] = []
        for raw, t in zip(raws, tgts):
            raw = raw.strip().lstrip("({").rstrip(")}").strip()
            if raw or t:
                out.append((raw, t))
        return out

    @staticmethod
    def _segments(cmd: str) -> list[list[str]]:
        return [shlex.split(Guard._blank_subst(raw), posix=True) for raw, _ in Guard._split(cmd)]

    def _strip(self, toks: list[str]) -> tuple[list[str], str | None, bool]:
        """Remove env assignments, shell keywords and wrappers. Returns (argv, sudo_user, used_sudo)."""
        sudo_user, used_sudo = None, False
        t = list(toks)
        while t:
            head = os.path.basename(t[0])
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t[0]) or t[0] in KEYWORDS:
                t.pop(0)
            elif head in WRAPPERS:
                t.pop(0)
                while t and t[0].startswith("-"):
                    t.pop(0)
            elif head == "timeout":
                t.pop(0)
                while t and t[0].startswith("-"):
                    t.pop(0)
                if t:
                    t.pop(0)
            elif head == "env":
                t.pop(0)
                while t and (t[0].startswith("-") or "=" in t[0]):
                    t.pop(0)
            elif head == "sudo":
                used_sudo, sudo_user = True, "root"
                t.pop(0)
                while t and t[0].startswith("-"):
                    flag = t.pop(0)
                    if flag in ("-u", "--user") and t:
                        sudo_user = t.pop(0)
                    elif flag.startswith("--user="):
                        sudo_user = flag.split("=", 1)[1]
                    elif flag in ("-g", "--group", "-h", "-p", "-r", "-t", "-C", "-D") and t:
                        t.pop(0)
            else:
                break
        return t, sudo_user, used_sudo

    @staticmethod
    def _deny_prog(prog: str, args: list[str]) -> Decision | None:
        if prog.startswith("mkfs"):
            return deny("no-system-admin", "host administration is out of scope")
        if prog == "sysctl" and "-w" in args:
            return deny("no-system-admin", "host administration is out of scope")
        if prog == "init" and args[:1] in (["0"], ["6"]):
            return deny("no-poweroff", "never power off or reboot the host")
        if prog == "git" and "clone" in args:
            return deny("no-network", "network access and package installs are not allowed during incidents")
        if prog == "crontab" and "-l" in args and not {"-e", "-r", "-i"} & set(args):
            return None  # listing is read-only
        hit = DENY_PROGS.get(prog)
        return deny(*hit) if hit else None

    def _check_tokens(self, toks: list[str]) -> Decision:
        argv, sudo_user, used_sudo = self._strip(toks)
        if not argv:
            if os.path.basename(toks[0]) == "env":  # bare `env` / `env X=1` prints the environment
                return deny("no-secrets", "reading credentials or the environment is not allowed")
            return ALLOW
        prog = os.path.basename(argv[0])
        args = argv[1:]
        hit = self._deny_prog(prog, args)
        if hit:
            return hit
        if used_sudo and sudo_user != "postgres" and prog not in SUDO_NON_PG:
            return deny("sudo-scope", "sudo is only for 'sudo -n -u postgres ...', systemctl on the PostgreSQL service, journalctl and dmesg")
        if used_sudo and INTERPRETER.fullmatch(prog):
            return deny("sudo-interpreter", "no inline interpreters under sudo; use psql and the provided helper scripts")
        if prog == "eval":
            return self.check_bash(" ".join(args))
        if prog in ("bash", "sh", "dash") and "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args):
                return self.check_bash(args[idx + 1])
        if prog == "systemctl":
            return self._check_systemctl(args)
        if prog in ("service", "pg_ctlcluster"):
            return deny("restart-budget", f"use 'sudo -n systemctl <verb> {self.cfg.pg_service}' so restarts are budgeted")
        if prog == "pg_ctl":
            if any(str(self.cfg.work_dir) in a for a in args):
                return ALLOW
            return self._check_pg_ctl_prod(args, used_sudo, sudo_user)
        if prog == "kill":
            return self._check_kill(args)
        if prog == "find":
            return self._check_find(args)
        if prog == "xargs":
            return self._check_xargs(args)
        files = [a for a in args if not a.startswith("-")]
        if prog in DELETERS:
            for f in files:
                d = self.path_write_allowed(f, delete=True)
                if not d.allow:
                    return d
            return ALLOW
        if prog in COMPRESSORS:
            for f in files:
                d = self.path_write_allowed(f, extra_roots=[str(self.cfg.pg_log_dir)])
                if not d.allow:
                    return d
            return ALLOW
        if prog == "mv":
            for f in files:
                d = self.path_write_allowed(f)
                if not d.allow:
                    return d
            return ALLOW
        if prog in ("cp", "install", "ln", "rsync"):
            if "-t" in args and args.index("-t") + 1 < len(args):
                dest = args[args.index("-t") + 1]
            else:
                dest = files[-1] if files else ""
            return self.path_write_allowed(dest)
        if prog == "tee":
            for f in files:
                d = self.path_write_allowed(f)
                if not d.allow:
                    return d
            return ALLOW
        if prog == "sed" and any(a == "--in-place" or (a.startswith("-i") and not a.startswith("--")) for a in args):
            for f in [a for a in files if "/" in a and not a.startswith("s/") and not a.startswith("s|")]:
                d = self.path_write_allowed(f)
                if not d.allow:
                    return d
            return ALLOW
        if prog in PATH_WRITERS:
            for f in [a for a in files if a.startswith(("/", "~"))]:
                d = self.path_write_allowed(f)
                if not d.allow:
                    return d
            return ALLOW
        if prog == "dd":
            for a in args:
                if a.startswith("of="):
                    d = self.path_write_allowed(a[3:])
                    if not d.allow:
                        return d
            return ALLOW
        if prog in ("pg_dump", "pg_dumpall", "pg_basebackup"):
            return self._check_output_flags(prog, args)
        if prog == "pg_archivecleanup":
            if "-n" in args:
                return ALLOW
            if len(files) >= 2 and files[0].rstrip("/") == "/var/lib/postgresql/wal_archive":
                return ALLOW
            return deny("archive-cleanup", "pg_archivecleanup is only allowed on /var/lib/postgresql/wal_archive with an explicit oldest-needed WAL file")
        return ALLOW

    # ------------------------------------------------------------------ the one allowed forced stop
    def _systemd_unit_state(self) -> tuple[str, float]:
        """(ActiveState, seconds spent in that state) of the PostgreSQL unit; ('unknown', 0) when systemd is not there."""
        try:
            r = subprocess.run(["systemctl", "show", self.cfg.pg_service, "-p", "ActiveState", "-p", "StateChangeTimestampMonotonic"], capture_output=True, text=True, timeout=5)
            kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
            since_us = int(kv.get("StateChangeTimestampMonotonic") or 0)
            return kv.get("ActiveState", "unknown"), (max(0.0, time.monotonic() - since_us / 1e6) if since_us else 0.0)
        except Exception:
            return "unknown", 0.0

    def _forced_stops_recent(self) -> int:
        now, n = self.now(), 0
        try:
            for line in self.cfg.restarts_path.read_text().splitlines():
                rec = json.loads(line)
                if rec.get("verb") == "forced-stop" and now - float(rec["ts"]) < 3600:
                    n += 1
        except Exception:
            pass
        return n

    def _check_pg_ctl_prod(self, args: list[str], used_sudo: bool, sudo_user: str | None) -> Decision:
        verb = mode = datadir = None
        i = 0
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else None
            if a in ("-m", "--mode") and nxt is not None:
                mode, i = nxt, i + 1
            elif a.startswith("--mode="):
                mode = a.split("=", 1)[1]
            elif a in ("-D", "--pgdata") and nxt is not None:
                datadir, i = nxt, i + 1
            elif a.startswith("--pgdata="):
                datadir = a.split("=", 1)[1]
            elif a.startswith("-D") and len(a) > 2:
                datadir = a[2:]
            elif not a.startswith("-") and verb is None:
                verb = a
            i += 1
        if verb in (None, "status", "reload"):
            return ALLOW  # read-only or equivalent to systemctl reload
        generic = (f"on the production cluster pg_ctl is only allowed as: sudo -n -u postgres pg_ctl -D {self.cfg.pgdata} stop -m immediate, and only for a HUNG shutdown (skill stuck-shutdown); "
                   f"otherwise use systemctl (the replica is controlled with: sudo -n systemctl start|stop|restart {self.cfg.standby_service})")
        if verb != "stop" or mode not in ("immediate", "i") or not datadir or os.path.normpath(datadir) != str(self.cfg.pgdata) or not (used_sudo and sudo_user == "postgres"):
            return deny("pg-ctl-prod", generic)
        state, age = self._unit_state()
        if state not in ("deactivating", "activating"):
            return deny("forced-stop-not-hung", f"the service is '{state}', not stuck stopping/starting; use 'systemctl restart' (a forced immediate stop is only for a hung shutdown)")
        if age < self.cfg.forced_stop_after_s:
            return deny("forced-stop-too-early", f"the unit has been '{state}' for {int(age)} s; wait until {int(self.cfg.forced_stop_after_s)} s (about {int(self.cfg.forced_stop_after_s - age)} s more), and keep gathering evidence")
        if self._forced_stops_recent() >= self.cfg.forced_stop_budget_per_hour:
            return deny("restart-budget", f"forced-stop budget exhausted ({self.cfg.forced_stop_budget_per_hour}/hour); escalate")
        try:
            self.cfg.restarts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.restarts_path, "a") as fh:
                fh.write(json.dumps({"ts": self.now(), "verb": "forced-stop"}) + "\n")
        except OSError:
            pass
        return ALLOW

    def _check_find(self, args: list[str]) -> Decision:
        if not any(a in FIND_RISKY for a in args):
            return ALLOW
        paths: list[str] = []
        for a in args:
            if a.startswith("-") or a in ("(", ")", "!"):
                break
            paths.append(a)
        for p in paths or ["."]:
            d = self.path_write_allowed(p, delete=True)
            if not d.allow:
                return deny("find-destructive", f"find with -delete/-exec is only allowed inside the work area ({d.reason})")
        for i, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                sub: list[str] = []
                for x in args[i + 1:]:
                    if x in (";", "+", "\\;"):
                        break
                    sub.append(paths[0] if x == "{}" and paths else x)
                if sub:
                    d = self._check_tokens(sub)
                    if not d.allow:
                        return d
        return ALLOW

    def _check_xargs(self, args: list[str]) -> Decision:
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in XARGS_VALUE_FLAGS else 1
        cmd = args[i:]
        if not cmd:
            return ALLOW
        if os.path.basename(cmd[0]) in XARGS_UNVERIFIABLE:
            return deny("xargs-unverifiable", f"xargs {os.path.basename(cmd[0])}: the arguments come from stdin and cannot be verified; pass explicit paths instead")
        return self._check_tokens(cmd)

    def _check_output_flags(self, prog: str, args: list[str]) -> Decision:
        outs: list[str] = []
        for i, a in enumerate(args):
            if a in ("-f", "--file", "-D", "--pgdata") and i + 1 < len(args):
                outs.append(args[i + 1])
            elif a.startswith(("--file=", "--pgdata=")):
                outs.append(a.split("=", 1)[1])
        for o in outs:
            if prog == "pg_basebackup" and o.startswith("/var/backups/postgresql/base/") and ".." not in o:
                continue
            d = self.path_write_allowed(o)
            if not d.allow:
                return d
        return ALLOW

    def _check_kill(self, args: list[str]) -> Decision:
        flags = [a for a in args if a.startswith("-")]
        pids = [a for a in args if not a.startswith("-") or a in ("-1",)]
        sig9 = any(re.fullmatch(r"-(9|KILL|SIGKILL|s9)", f) for f in flags) or any("KILL" in f.upper() or f == "-s9" for f in flags)
        postmaster = ""
        try:
            postmaster = (self.cfg.pgdata / "postmaster.pid").read_text().splitlines()[0].strip()
        except Exception:
            pass
        for p in pids:
            if p in ("1", "-1", "0", postmaster):
                return deny("kill-postmaster", "never signal the postmaster, init, or a process group")
        if sig9:
            return deny("kill-9", "SIGKILL on PostgreSQL processes triggers crash recovery; use pg_terminate_backend or pg_cancel_backend")
        return ALLOW

    def _recent_restarts(self) -> list[float]:
        now, out = self.now(), []
        try:
            for line in self.cfg.restarts_path.read_text().splitlines():
                t = float(json.loads(line)["ts"])
                if now - t < 3600:
                    out.append(t)
        except Exception:
            pass
        return out

    def restarts_left(self) -> int:
        return max(0, self.cfg.restart_budget_per_hour - len(self._recent_restarts()))

    def _check_systemctl(self, args: list[str]) -> Decision:
        verbs = [a for a in args if not a.startswith("-")]
        if not verbs:
            return ALLOW
        verb, units = verbs[0], verbs[1:]
        if verb in ("status", "is-active", "is-enabled", "show", "list-units", "cat"):
            return ALLOW
        if any("raiden" in u for u in units):
            return deny("self-preservation", "the agent may not control its own service")
        if verb not in ("start", "stop", "restart", "reload", "try-restart", "reload-or-restart"):
            return deny("systemctl-verb", f"systemctl {verb} is not allowed")
        standby = {self.cfg.standby_service, self.cfg.standby_service + ".service"}
        if units and all(u in standby for u in units):
            return ALLOW  # the streaming replica is not the primary: no restart budget
        if units != [self.cfg.pg_service]:
            return deny("systemctl-unit", f"only the {self.cfg.pg_service} unit (and the {self.cfg.standby_service} replica) may be controlled")
        if verb == "reload":
            return ALLOW
        if self.restarts_left() <= 0:
            return deny("restart-budget", f"restart budget exhausted ({self.cfg.restart_budget_per_hour}/hour); escalate or use reload")
        try:
            self.cfg.restarts_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cfg.restarts_path, "a") as fh:
                fh.write(json.dumps({"ts": self.now(), "verb": verb}) + "\n")
        except OSError:
            pass
        return ALLOW

    # ------------------------------------------------------------------ SQL inside commands / scripts
    def _check_sql(self, text: str, clone: bool) -> Decision:
        for raw in re.split(r";", text):
            stmt = " ".join(raw.split()).strip().strip("\"'")
            low = stmt.lower()
            if not low:
                continue
            m = re.search(r"\bdrop\s+(database|schema|tablespace)\b", low)
            if m and not clone:
                return deny("sql-drop", f"DROP {m.group(1).upper()} is not allowed")
            m = re.search(r"\bdrop\s+table\s+(.*)$", low)
            if m and not clone and not SAFE_TABLE.search(m.group(1)):
                return deny("sql-drop-table", "DROP TABLE is only allowed for scratch tables (tmp_/temp_/scratch_/restore_/quarantine_/pitr_ prefixes)")
            m = re.search(r"\btruncate\s+(?:table\s+)?(?:only\s+)?(.*)$", low)
            if m and not clone and not SAFE_TABLE.search(m.group(1)):
                return deny("sql-truncate", "TRUNCATE is not allowed on application tables")
            m = re.search(r"(?:^|\bpsql\b.*?-c\s+[\"']?)(delete\s+from|update)\s+([\w.\"]+)", low)
            if m and " where " not in f" {low} " and not clone and not SAFE_TABLE.search(m.group(2)):
                return deny("sql-no-where", "DELETE/UPDATE without WHERE on a non-scratch table is not allowed")
            if "pg_terminate_backend" in low and "from pg_stat_activity" in low and " where " not in f" {low} ":
                return deny("sql-mass-terminate", "pg_terminate_backend over all sessions is not allowed; filter by state/age/application")
        return ALLOW
