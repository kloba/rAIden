"""Human-readable values for titles, messages and the AI's notes (the web UI formats on its own)."""

from __future__ import annotations

from .catalog import BY_ID


def plural(n: int, word: str) -> str:
    """'1 server', '7 servers'."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def category(c: str) -> str:
    """A metric or incident category as people say it (the web UI's categoryLabel)."""
    return {"cpu": "CPU", "io": "I/O", "wal": "WAL", "xid": "Transaction IDs"}.get(c, c[:1].upper() + c[1:].replace("_", " "))


def duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 90:
        return f"{s} s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r} s" if r and m < 10 else "")
    if s < 86400 * 2:
        h, r = divmod(s, 3600)
        return f"{h} h" + (f" {r // 60} min" if r >= 60 and h < 10 else "")
    return f"{s / 86400:.1f} days"


def size(n: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    v, i = float(n), 0
    while abs(v) >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


def big(n: float) -> str:
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f} billion"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f} million"
    if abs(n) >= 1e4:
        return f"{n / 1e3:.1f}k"
    return f"{n:,.0f}" if abs(n) >= 100 else f"{n:.3g}"


def value(metric: str, v: float | None) -> str:
    if v is None:
        return "no data"
    unit = BY_ID[metric].unit if metric in BY_ID else ""
    if unit == "percent":
        return f"{v:.0f}%" if v >= 10 else f"{v:.1f}%"
    if unit == "seconds":
        return duration(v)
    if unit in ("bytes",):
        return size(v)
    if unit == "bytes_per_second":
        return size(v) + "/s"
    if unit == "per_second":
        return f"{big(v)}/s"
    if unit == "xids":
        return big(v)
    if unit == "count":
        return f"{round(v):,}" if abs(v) < 1e4 else big(v)
    return big(v)
