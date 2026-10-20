"""Deterministic patrol: run the checks in checks.d/ (no LLM); only WARN/CRIT wakes the agent."""

from __future__ import annotations

import re
import subprocess

from .config import Config
from .events import utcnow_iso
from .incident import Incident

VERDICT = re.compile(r"^(OK|WARN|CRIT)\s+([\w.\-]+):?\s*(.*)$")


def run_checks(cfg: Config, timeout_s: int = 20) -> list[dict]:
    results: list[dict] = []
    env = {"PATH": cfg.path_env, "HOME": str(cfg.state_dir), "PGCONNECT_TIMEOUT": "3", "LANG": "C.UTF-8"}
    if not cfg.checks_dir.is_dir():
        return results
    for script in sorted(cfg.checks_dir.glob("*.sh")):
        status, name, message = "CRIT", script.stem, "check produced no verdict line"
        try:
            r = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=timeout_s, env=env, cwd=str(cfg.workdir))
            last = (r.stdout.strip().splitlines() or [""])[-1]
            m = VERDICT.match(last)
            if m:
                status, name, message = m.group(1), m.group(2), m.group(3)
            else:
                message = f"unparseable output (exit {r.returncode}): {last[:120]}"
        except subprocess.TimeoutExpired:
            message = f"timed out after {timeout_s}s"
        results.append({"script": script.name, "status": status, "name": name, "message": message})
    return results


def worst(results: list[dict]) -> str:
    return "CRIT" if any(r["status"] == "CRIT" for r in results) else "WARN" if any(r["status"] == "WARN" for r in results) else "OK"


def patrol_incident(results: list[dict]) -> Incident | None:
    bad = [r for r in results if r["status"] != "OK"]
    if not bad:
        return None
    now = utcnow_iso()
    sigs = {
        f"patrol:{r['name']}": {"count": 1, "kind": "patrol", "first": now, "last": now, "sample": f"{r['status']} {r['name']}: {r['message']}"}
        for r in bad
    }
    iid = "inc-" + now.replace("-", "").replace(":", "")[:15] + "-pat"
    return Incident(
        id=iid, opened_ts=0.0, opened_iso=now,
        reason="patrol: " + ", ".join(f"{r['status']} {r['name']}" for r in bad[:4]),
        kinds={"patrol": len(bad)}, signatures=sigs, events_total=len(bad),
        sample=[f"{r['status']} {r['name']}: {r['message']}" for r in bad],
        critical=any(r["status"] == "CRIT" for r in bad),
    )


def log_patrol(cfg: Config, results: list[dict]) -> None:
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    line = f"{utcnow_iso()} worst={worst(results)} " + " ".join(f"{r['name']}={r['status']}" for r in results)
    with open(cfg.memory_dir / "patrol.log", "a") as fh:
        fh.write(line + "\n")
