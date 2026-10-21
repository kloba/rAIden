"""A small, dependency-free cron: five fields (minute hour day-of-month month day-of-week), lists, ranges, steps, names,
@hourly/@daily/@weekly/@monthly, time zones. Day-of-month and day-of-week combine like Vixie cron: when both are
restricted, either one matching is enough."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *",
           "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DAYS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int]:
    out: set[int] = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) == 0:
                raise CronError(f"bad step in {text!r}")
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names), _num(y, names)
        else:
            a = _num(part, names)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise CronError(f"{text!r} is out of range {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return out


def _num(x: str, names: dict[str, int] | None) -> int:
    if names and x in names:
        return names[x]
    if not x.isdigit():
        raise CronError(f"{x!r} is not a number")
    return int(x)


class Cron:
    def __init__(self, expr: str) -> None:
        self.expr = expr.strip()
        fields = ALIASES.get(self.expr.lower(), self.expr).split()
        if len(fields) != 5:
            raise CronError("a cron expression has five fields: minute hour day-of-month month day-of-week")
        self.minutes = _field(fields[0], *BOUNDS[0])
        self.hours = _field(fields[1], *BOUNDS[1])
        self.dom = _field(fields[2], *BOUNDS[2])
        self.months = _field(fields[3], *BOUNDS[3], MONTHS)
        dow = _field(fields[4], *BOUNDS[4], DAYS)
        self.dow = {d % 7 for d in dow}
        self.dom_any = fields[2] == "*"
        self.dow_any = fields[4] == "*"
        self.fields = fields

    def _day_ok(self, d: datetime) -> bool:
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        if self.dom_any and self.dow_any:
            return True
        if self.dom_any:
            return dow_ok
        if self.dow_any:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime, tz: str = "UTC") -> datetime:
        """Next fire time strictly after `after` (naive UTC in, naive UTC out)."""
        zone = ZoneInfo(tz) if tz and tz != "UTC" else timezone.utc
        local = after.replace(tzinfo=timezone.utc).astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(366 * 24 * 60 // 1):
            if local.month not in self.months:
                local = (local.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
                continue
            if not self._day_ok(local):
                local = local.replace(hour=0, minute=0) + timedelta(days=1)
                continue
            if local.hour not in self.hours:
                local = local.replace(minute=0) + timedelta(hours=1)
                continue
            if local.minute not in self.minutes:
                local += timedelta(minutes=1)
                continue
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        raise CronError("no fire time within a year")

    def describe(self) -> str:
        m, h, dom, mon, dow = self.fields
        if m.startswith("*/") and h == "*" and dom == mon == dow == "*":
            return f"Every {m[2:]} minutes"
        if m == "*" and h == "*":
            return "Every minute"
        if m.isdigit() and h == "*" and dom == mon == dow == "*":
            return f"Every hour at :{int(m):02d}"
        if m.isdigit() and h.startswith("*/") and dom == mon == dow == "*":
            return f"Every {h[2:]} hours at :{int(m):02d}"
        if m.isdigit() and h.isdigit():
            at = f"{int(h):02d}:{int(m):02d}"
            if dom == mon == "*" and dow == "*":
                return f"Every day at {at}"
            if dom == mon == "*" and dow in ("1-5", "mon-fri"):
                return f"Weekdays at {at}"
            if dom == mon == "*" and len(self.dow) == 1:
                return f"Every {DAY_NAMES[next(iter(self.dow))]} at {at}"
            if dom.isdigit() and mon == dow == "*":
                return f"Monthly on day {dom} at {at}"
        return self.expr


def validate(expr: str) -> str | None:
    try:
        Cron(expr)
    except CronError as exc:
        return str(exc)
    return None
