"""Console settings, read from the environment (see deploy/console/console.env.example)."""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def _default_data_dir() -> Path:
    if os.geteuid() == 0 if hasattr(os, "geteuid") else False:
        return Path("/var/lib/raiden-console")
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "raiden-console"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message (and the exception, if any)."""

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}", "level": record.levelname.lower(),
               "logger": record.name, "message": record.getMessage()}
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


@dataclass(frozen=True)
class ConsoleSettings:
    data_dir: Path
    database_url: str
    host: str = "127.0.0.1"
    port: int = 8080
    secret_key: str = ""            # empty: a key is generated once in data_dir/secret.key (0600)
    demo: bool = False              # simulated fleet + scripted model, nothing real is touched
    demo_speed: float = 1.0         # >1 makes demo scenarios happen sooner
    static_dir: Path | None = None  # built web UI (web/dist); None = look next to the package
    base_url: str = ""              # public URL, used in notification links
    cookie_secure: bool = False     # set when the console is served over HTTPS
    session_hours: int = 12
    max_sessions: int = 3           # concurrent AI sessions (incidents + routines; chat is not counted)
    log_level: str = "info"
    log_format: str = "text"        # text | json (one object per line, for a log pipeline)

    @classmethod
    def from_env(cls, **overrides: object) -> "ConsoleSettings":
        e = os.environ.get
        # `raiden console --data DIR` moves the SQLite database too, not only the secret key
        data_dir = Path(overrides.get("data_dir") or e("RAIDEN_CONSOLE_DATA", "") or _default_data_dir())  # type: ignore[arg-type]
        values: dict[str, object] = dict(
            data_dir=data_dir,
            database_url=e("RAIDEN_CONSOLE_DB", "") or f"sqlite:///{data_dir / 'console.db'}",
            host=e("RAIDEN_CONSOLE_HOST", "127.0.0.1"),
            port=int(e("RAIDEN_CONSOLE_PORT", "8080")),
            secret_key=e("RAIDEN_SECRET_KEY", ""),
            demo=_flag("RAIDEN_DEMO"),
            demo_speed=float(e("RAIDEN_DEMO_SPEED", "1")),
            static_dir=Path(e("RAIDEN_CONSOLE_STATIC")) if e("RAIDEN_CONSOLE_STATIC") else None,
            base_url=e("RAIDEN_BASE_URL", "").rstrip("/"),
            cookie_secure=_flag("RAIDEN_COOKIE_SECURE", e("RAIDEN_BASE_URL", "").startswith("https://")),
            session_hours=int(e("RAIDEN_SESSION_HOURS", "12")),
            max_sessions=int(e("RAIDEN_MAX_AI_SESSIONS", "3")),
            log_level=e("RAIDEN_LOG_LEVEL", "info"),
            log_format=e("RAIDEN_LOG_FORMAT", "text").strip().lower(),
        )
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)  # type: ignore[arg-type]

    def configure_logging(self) -> None:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter() if self.log_format == "json" else logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        root = logging.getLogger()
        root.handlers[:] = [handler]
        root.setLevel(getattr(logging, self.log_level.upper(), logging.INFO))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")
