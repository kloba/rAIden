"""rAIden command line: run the daemon, replay a log, run patrol, self-test the guardrails, doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

from .config import Config


def _print_table(rows: list[tuple[str, ...]], widths: tuple[int, ...]) -> None:
    for row in rows:
        print("  ".join(str(c)[:w].ljust(w) for c, w in zip(row, widths)).rstrip())


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from .daemon import Daemon

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        asyncio.run(Daemon(cfg, from_start=args.from_start).run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_replay(cfg: Config, args: argparse.Namespace) -> int:
    """Classify and aggregate a recorded jsonlog file offline: which incidents would have woken the agent?"""
    from .incident import Aggregator, classify, parse_jsonlog_line

    clock = [0.0]
    agg = Aggregator(cfg.trigger_weight, cfg.quiet_s, cfg.max_wait_s, cfg.suppress_s, clock=lambda: clock[0])
    incidents, total, interesting = [], 0, 0
    for line in Path(args.file).read_text(errors="replace").splitlines():
        ev = parse_jsonlog_line(line)
        if not ev:
            continue
        total += 1
        classify(ev)
        clock[0] = ev.ts
        inc = agg.poll()
        if inc:
            incidents.append(inc)
        if agg.feed(ev):
            interesting += 1
    clock[0] += cfg.max_wait_s + 1
    inc = agg.poll()
    if inc:
        incidents.append(inc)
    print(f"{total} log lines, {interesting} relevant, {len(incidents)} incident(s) would wake rAIden")
    for inc in incidents:
        print(f"\n{inc.prompt_block()}")
    if args.handle and incidents:
        from .daemon import Daemon

        logging.basicConfig(level=logging.INFO, stream=sys.stdout)
        daemon = Daemon(cfg)
        res = asyncio.run(daemon.handle(incidents[0]))
        print(json.dumps(res.__dict__ if res else None, indent=2, default=str))
    return 0


def cmd_once(cfg: Config, args: argparse.Namespace) -> int:
    """Hand-made incident: `raiden once "FATAL: could not open file ..." --critical`."""
    from .daemon import Daemon
    from .events import utcnow_iso
    from .incident import Incident

    now = utcnow_iso()
    sigs = {f"manual:{i}": {"count": 1, "kind": "manual", "first": now, "last": now, "sample": t} for i, t in enumerate(args.text)}
    inc = Incident(
        id="inc-" + now.replace("-", "").replace(":", "")[:15] + "-man", opened_ts=0.0, opened_iso=now,
        reason="manual trigger", kinds={"manual": len(args.text)}, signatures=sigs, events_total=len(args.text),
        sample=list(args.text), critical=args.critical,
    )
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    res = asyncio.run(Daemon(cfg).handle(inc))
    print(json.dumps(res.__dict__ if res else None, indent=2, default=str))
    return 0 if res and res.resolution in ("resolved", "mitigated") else 1


def cmd_evolve(cfg: Config, args: argparse.Namespace) -> int:
    """Run only the evolve session for an already closed incident."""
    from .daemon import Daemon
    from .incident import Incident

    idir = cfg.incidents_dir / args.incident
    meta = json.loads((idir / "incident.json").read_text())
    inc = Incident(
        id=meta["id"], opened_ts=0.0, opened_iso=meta.get("opened_at", ""), reason=meta.get("reason", ""), kinds=meta.get("kinds", {}),
        signatures={}, events_total=meta.get("events_count", 0), sample=[], critical=True,
    )
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    asyncio.run(Daemon(cfg)._evolve(inc, idir))
    return 0


def cmd_patrol(cfg: Config, args: argparse.Namespace) -> int:
    from .patrol import run_checks, worst

    results = run_checks(cfg)
    _print_table([("STATUS", "CHECK", "MESSAGE")] + [(r["status"], r["name"], r["message"]) for r in results], (6, 28, 110))
    w = worst(results)
    return {"OK": 0, "WARN": 1, "CRIT": 2}[w]


def cmd_selftest(cfg: Config, args: argparse.Namespace) -> int:
    from . import selftest
    from .guardrails import Guard

    results = selftest.run(Guard(cfg), str(cfg.state_dir), str(cfg.workdir), str(cfg.pgdata))
    failures = [(c, d) for c, d, ok in results if not ok]
    if args.verbose or failures:
        for c, d, ok in results:
            if args.verbose or not ok:
                got = "allow" if d.allow else f"deny[{d.rule}]"
                want = "allow" if c.allow else f"deny[{c.rule or '*'}]"
                print(f"{'ok  ' if ok else 'FAIL'} {c.name:<42} want {want:<28} got {got}")
    print(f"guardrail self-test: {len(results) - len(failures)}/{len(results)} cases pass")
    return 1 if failures else 0


def _step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'ok' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    return ok


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .memory import skill_index
    from .patrol import run_checks

    good = True
    good &= _step("config", bool(cfg.endpoint), f"model={cfg.deployment} endpoint={cfg.endpoint or '(RAIDEN_AOAI_ENDPOINT not set)'}")
    logs = sorted(cfg.pg_log_dir.glob(cfg.pg_log_glob)) if cfg.pg_log_dir.is_dir() else []
    good &= _step("postgres jsonlog", bool(logs), f"{len(logs)} file(s) in {cfg.pg_log_dir}")
    r = subprocess.run(["pg_isready", "-q", "-h", "/var/run/postgresql"], capture_output=True)
    good &= _step("pg_isready", r.returncode == 0)
    r = subprocess.run(["psql", "-X", "-Atc", "select 'connected as ' || current_user || ', superuser=' || current_setting('is_superuser')", "-d", "postgres"], capture_output=True, text=True, env={"PATH": cfg.path_env, "PGCONNECT_TIMEOUT": "3"})
    good &= _step("psql as the agent role", r.returncode == 0, (r.stdout or r.stderr).strip()[:100])
    skills = skill_index(cfg)
    good &= _step("skills", len(skills) > 0, f"{len(skills)} loaded")
    checks = list(cfg.checks_dir.glob("*.sh")) if cfg.checks_dir.is_dir() else []
    good &= _step("patrol checks", len(checks) > 0, f"{len(checks)} scripts")
    for d in (cfg.state_dir, cfg.work_dir, cfg.memory_dir, cfg.incidents_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".doctor"
            probe.write_text("x")
            probe.unlink()
            good &= _step(f"writable {d}", True)
        except OSError as exc:
            good &= _step(f"writable {d}", False, str(exc))
    if args.patrol:
        res = run_checks(cfg)
        for x in res:
            _step(f"patrol {x['name']}", x["status"] == "OK", f"{x['status']} {x['message']}"[:100])
    rc = cmd_selftest(cfg, argparse.Namespace(verbose=False))
    good &= rc == 0
    if args.llm:
        from .llm import AzureLLM

        try:
            resp = AzureLLM(cfg).complete_sync([{"role": "user", "content": "Reply with the single word: ready"}], max_completion_tokens=400)
            good &= _step("llm", True, f"{cfg.deployment}: {(resp.choices[0].message.content or '').strip()[:40]!r}")
        except Exception as exc:
            good &= _step("llm", False, f"{exc.__class__.__name__}: {str(exc)[:160]}")
    return 0 if good else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="raiden", description="rAIden: the autonomous on-call DBA for PostgreSQL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="watch the PostgreSQL log 24/7 and handle incidents")
    p.add_argument("--from-start", action="store_true", help="process the newest log file from its beginning")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("replay", help="which incidents would a recorded jsonlog file trigger?")
    p.add_argument("file")
    p.add_argument("--handle", action="store_true", help="also let the agent handle the first incident (uses the LLM)")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("once", help="handle a hand-made incident")
    p.add_argument("text", nargs="+")
    p.add_argument("--critical", action="store_true")
    p.set_defaults(fn=cmd_once)

    p = sub.add_parser("evolve", help="run the evolve session for a closed incident")
    p.add_argument("incident")
    p.set_defaults(fn=cmd_evolve)

    p = sub.add_parser("patrol", help="run the deterministic checks once")
    p.set_defaults(fn=cmd_patrol)

    p = sub.add_parser("selftest", help="run the guardrail regression matrix")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser("doctor", help="check the installation")
    p.add_argument("--llm", action="store_true", help="also make one model call")
    p.add_argument("--patrol", action="store_true", help="also run the patrol checks")
    p.set_defaults(fn=cmd_doctor)

    args = parser.parse_args(argv)
    return args.fn(Config.from_env(), args)


if __name__ == "__main__":
    raise SystemExit(main())
