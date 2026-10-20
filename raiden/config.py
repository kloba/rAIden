"""Runtime configuration, read from the environment (see deploy/vm/raiden.env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- layout -----------------------------------------------------------------------------
    workdir: Path = Path("/opt/raiden")
    state_dir: Path = Path("/var/lib/raiden")
    pg_log_dir: Path = Path("/var/log/postgresql")
    pg_log_glob: str = "raiden-*.json"
    pg_service: str = "postgresql@18-main"
    pgdata: Path = Path("/var/lib/postgresql/18/main")
    pg_conf_dir: Path = Path("/etc/postgresql/18/main")
    # --- LLM: Azure OpenAI with the VM's managed identity (no API keys) ----------------------
    endpoint: str = ""
    deployment: str = "gpt-5.5"
    api_version: str = "2025-04-01-preview"
    reasoning_effort: str = "medium"
    price_in_per_m: float = 0.0   # optional, only used to estimate cost
    price_out_per_m: float = 0.0
    # --- incident policy ---------------------------------------------------------------------
    quiet_s: float = 8.0          # no new trigger events for this long closes the incident window
    max_wait_s: float = 30.0      # ... or the window is closed after this long
    trigger_weight: int = 10
    suppress_s: float = 300.0     # after an incident, repeat app-level errors with the same signature are muted
    verify_window_s: float = 45.0
    max_turns: int = 60
    incident_timeout_s: float = 900.0
    evolve_enabled: bool = True
    evolve_max_turns: int = 40
    evolve_timeout_s: float = 600.0
    restart_budget_per_hour: int = 6
    forced_stop_after_s: float = 240.0      # a stuck 'deactivating' unit must be this old before pg_ctl stop -m immediate is allowed
    forced_stop_budget_per_hour: int = 1
    # --- liveness / patrol -------------------------------------------------------------------
    liveness_interval_s: float = 2.0
    liveness_down_after_s: float = 20.0
    patrol_interval_s: float = 1800.0   # 0 disables the periodic patrol
    patrol_timeout_s: int = 120
    patrol_min_gap_s: float = 20.0      # at least this long between two patrol runs, however many reloads

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        base = cls()
        return cls(
            workdir=Path(e("RAIDEN_WORKDIR", str(base.workdir))),
            state_dir=Path(e("RAIDEN_STATE_DIR", str(base.state_dir))),
            pg_log_dir=Path(e("RAIDEN_PG_LOG_DIR", str(base.pg_log_dir))),
            pg_log_glob=e("RAIDEN_PG_LOG_GLOB", base.pg_log_glob),
            pg_service=e("RAIDEN_PG_SERVICE", base.pg_service),
            pgdata=Path(e("RAIDEN_PGDATA", str(base.pgdata))),
            pg_conf_dir=Path(e("RAIDEN_PG_CONF_DIR", str(base.pg_conf_dir))),
            endpoint=e("RAIDEN_AOAI_ENDPOINT", base.endpoint),
            deployment=e("RAIDEN_AOAI_DEPLOYMENT", base.deployment),
            api_version=e("RAIDEN_AOAI_API_VERSION", base.api_version),
            reasoning_effort=e("RAIDEN_REASONING_EFFORT", base.reasoning_effort),
            price_in_per_m=float(e("RAIDEN_PRICE_IN_PER_M", base.price_in_per_m)),
            price_out_per_m=float(e("RAIDEN_PRICE_OUT_PER_M", base.price_out_per_m)),
            quiet_s=float(e("RAIDEN_QUIET_S", base.quiet_s)),
            max_wait_s=float(e("RAIDEN_MAX_WAIT_S", base.max_wait_s)),
            trigger_weight=int(e("RAIDEN_TRIGGER_WEIGHT", base.trigger_weight)),
            suppress_s=float(e("RAIDEN_SUPPRESS_S", base.suppress_s)),
            verify_window_s=float(e("RAIDEN_VERIFY_WINDOW_S", base.verify_window_s)),
            max_turns=int(e("RAIDEN_MAX_TURNS", base.max_turns)),
            incident_timeout_s=float(e("RAIDEN_INCIDENT_TIMEOUT_S", base.incident_timeout_s)),
            evolve_enabled=e("RAIDEN_EVOLVE", "1") not in ("0", "false", "no"),
            evolve_max_turns=int(e("RAIDEN_EVOLVE_MAX_TURNS", base.evolve_max_turns)),
            evolve_timeout_s=float(e("RAIDEN_EVOLVE_TIMEOUT_S", base.evolve_timeout_s)),
            restart_budget_per_hour=int(e("RAIDEN_RESTART_BUDGET", base.restart_budget_per_hour)),
            forced_stop_after_s=float(e("RAIDEN_FORCED_STOP_AFTER_S", base.forced_stop_after_s)),
            forced_stop_budget_per_hour=int(e("RAIDEN_FORCED_STOP_BUDGET", base.forced_stop_budget_per_hour)),
            liveness_interval_s=float(e("RAIDEN_LIVENESS_INTERVAL_S", base.liveness_interval_s)),
            liveness_down_after_s=float(e("RAIDEN_LIVENESS_DOWN_AFTER_S", base.liveness_down_after_s)),
            patrol_interval_s=float(e("RAIDEN_PATROL_INTERVAL_S", base.patrol_interval_s)),
            patrol_timeout_s=int(e("RAIDEN_PATROL_TIMEOUT_S", base.patrol_timeout_s)),
            patrol_min_gap_s=float(e("RAIDEN_PATROL_MIN_GAP_S", base.patrol_min_gap_s)),
        )

    # --- derived paths -----------------------------------------------------------------------
    @property
    def incidents_dir(self) -> Path:
        return self.state_dir / "incidents"

    @property
    def work_dir(self) -> Path:
        return self.state_dir / "work"

    @property
    def memory_dir(self) -> Path:
        return self.state_dir / "memory"

    @property
    def lessons_path(self) -> Path:
        return self.memory_dir / "lessons.md"

    @property
    def evolution_path(self) -> Path:
        return self.memory_dir / "evolution.md"

    @property
    def escalations_dir(self) -> Path:
        return self.state_dir / "escalations"

    @property
    def quarantine_dir(self) -> Path:
        return self.state_dir / "quarantine"

    @property
    def events_path(self) -> Path:
        return self.state_dir / "events.jsonl"

    @property
    def audit_path(self) -> Path:
        return self.state_dir / "audit.jsonl"

    @property
    def restarts_path(self) -> Path:
        return self.state_dir / "restarts.jsonl"

    @property
    def skills_dir(self) -> Path:
        return self.workdir / ".claude" / "skills"

    @property
    def checks_dir(self) -> Path:
        return self.workdir / "checks.d"

    @property
    def path_env(self) -> str:
        return "/usr/lib/postgresql/18/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
