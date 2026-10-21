"""Metric storage: one JSON snapshot per server and collection, 15-minute rollups for the long view, a hot in-memory window
for monitors and live charts. Portable across SQLite and PostgreSQL (no JSON SQL functions needed)."""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Any, Iterable

from sqlalchemy import delete, func, select

from .db import Database
from .models import MetricRollup, MetricSnapshot

ROLLUP_S = 900
RAW_MAX_RANGE_S = 2 * 86400  # longer ranges read the rollups
HOT_WINDOW_S = 3 * 3600


class MetricStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._hot: dict[str, deque[tuple[int, dict[str, float]]]] = defaultdict(deque)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------------------------- write
    def append(self, server_id: str, ts: int, data: dict[str, float]) -> None:
        clean = {k: round(float(v), 4) for k, v in data.items() if v is not None and isinstance(v, (int, float)) and math.isfinite(float(v))}
        with self.db.session() as s:
            s.add(MetricSnapshot(server_id=server_id, ts=int(ts), data=clean))
        self._remember(server_id, int(ts), clean)

    def append_many(self, server_id: str, rows: Iterable[tuple[int, dict[str, float]]]) -> None:
        """Bulk insert (demo history, host-agent backfill); the hot window keeps the recent part."""
        batch = [MetricSnapshot(server_id=server_id, ts=int(ts), data=d) for ts, d in rows]
        with self.db.session() as s:
            s.add_all(batch)
        for snap in batch[-int(HOT_WINDOW_S / 10):]:
            self._remember(server_id, snap.ts, snap.data)

    def _remember(self, server_id: str, ts: int, data: dict[str, float]) -> None:
        with self._lock:
            q = self._hot[server_id]
            if q and ts <= q[-1][0]:
                return
            q.append((ts, data))
            while q and ts - q[0][0] > HOT_WINDOW_S:
                q.popleft()

    def forget(self, server_id: str) -> None:
        with self._lock:
            self._hot.pop(server_id, None)

    # ------------------------------------------------------------------------------------------- read (hot)
    def warm(self, server_id: str, now: int) -> None:
        with self._lock:
            if self._hot.get(server_id):
                return
        with self.db.session() as s:
            rows = s.execute(
                select(MetricSnapshot.ts, MetricSnapshot.data)
                .where(MetricSnapshot.server_id == server_id, MetricSnapshot.ts >= now - HOT_WINDOW_S)
                .order_by(MetricSnapshot.ts)
            ).all()
        for ts, data in rows:
            self._remember(server_id, ts, data)

    def latest(self, server_id: str) -> tuple[int, dict[str, float]] | None:
        with self._lock:
            q = self._hot.get(server_id)
            if q:
                return q[-1]
        with self.db.session() as s:
            row = s.execute(
                select(MetricSnapshot.ts, MetricSnapshot.data).where(MetricSnapshot.server_id == server_id).order_by(MetricSnapshot.ts.desc()).limit(1)
            ).first()
        return (row[0], row[1]) if row else None

    def window(self, server_id: str, seconds: int, now: int) -> list[tuple[int, dict[str, float]]]:
        with self._lock:
            q = list(self._hot.get(server_id, ()))
        return [(ts, d) for ts, d in q if ts >= now - seconds]

    # ------------------------------------------------------------------------------------------- read (charts)
    def series(
        self, server_id: str, keys: list[str], start: int, end: int, max_points: int = 360, agg: str = "avg"
    ) -> dict[str, list[list[float]]]:
        """{key: [[ts, value], ...]} bucketed to at most max_points; missing keys come back empty."""
        span = max(1, end - start)
        out: dict[str, list[list[float]]] = {k: [] for k in keys}
        if span <= RAW_MAX_RANGE_S:
            with self.db.session() as s:
                rows = s.execute(
                    select(MetricSnapshot.ts, MetricSnapshot.data)
                    .where(MetricSnapshot.server_id == server_id, MetricSnapshot.ts >= start, MetricSnapshot.ts <= end)
                    .order_by(MetricSnapshot.ts)
                ).all()
            samples = [(ts, {k: d[k] for k in keys if k in d}) for ts, d in rows]
            step = max(1, math.ceil(span / max_points))
        else:
            with self.db.session() as s:
                rows = s.execute(
                    select(MetricRollup.ts, MetricRollup.data)
                    .where(MetricRollup.server_id == server_id, MetricRollup.ts >= start, MetricRollup.ts <= end)
                    .order_by(MetricRollup.ts)
                ).all()
            pick = {"avg": 0, "max": 1, "min": 2}.get(agg, 0)
            samples = [(ts, {k: d[k][pick if len(d[k]) > pick else 0] for k in keys if k in d}) for ts, d in rows]
            step = max(ROLLUP_S, math.ceil(span / max_points))
        return _bucket(samples, keys, start, step, agg, out)

    def latest_values(self, server_id: str) -> dict[str, float]:
        snap = self.latest(server_id)
        return dict(snap[1]) if snap else {}

    def keys_for(self, server_id: str) -> list[str]:
        snap = self.latest(server_id)
        return sorted(snap[1]) if snap else []

    # ------------------------------------------------------------------------------------------- maintenance
    def rollup(self, now: int) -> int:
        """Aggregate every finished 15-minute bucket that has raw data but no rollup yet."""
        done = 0
        end = now - now % ROLLUP_S
        with self.db.session() as s:
            servers = [r[0] for r in s.execute(select(MetricSnapshot.server_id).distinct()).all()]
            for sid in servers:
                last = s.execute(select(func.max(MetricRollup.ts)).where(MetricRollup.server_id == sid)).scalar()
                first_raw = s.execute(select(func.min(MetricSnapshot.ts)).where(MetricSnapshot.server_id == sid)).scalar()
                if first_raw is None:
                    continue
                begin = (last + ROLLUP_S) if last is not None else first_raw - first_raw % ROLLUP_S
                if begin >= end:
                    continue
                rows = s.execute(
                    select(MetricSnapshot.ts, MetricSnapshot.data)
                    .where(MetricSnapshot.server_id == sid, MetricSnapshot.ts >= begin, MetricSnapshot.ts < end)
                    .order_by(MetricSnapshot.ts)
                ).all()
                buckets: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
                for ts, data in rows:
                    b = ts - ts % ROLLUP_S
                    for k, v in data.items():
                        buckets[b][k].append(v)
                for b, values in sorted(buckets.items()):
                    s.add(MetricRollup(server_id=sid, ts=b, data={k: [round(sum(v) / len(v), 4), max(v), min(v)] for k, v in values.items()}))
                    done += 1
        return done

    def prune(self, now: int, raw_days: float, rollup_days: float) -> None:
        with self.db.session() as s:
            s.execute(delete(MetricSnapshot).where(MetricSnapshot.ts < now - int(raw_days * 86400)))
            s.execute(delete(MetricRollup).where(MetricRollup.ts < now - int(rollup_days * 86400)))


def _bucket(
    samples: list[tuple[int, dict[str, float]]], keys: list[str], start: int, step: int, agg: str, out: dict[str, list[list[float]]]
) -> dict[str, list[list[float]]]:
    acc: dict[str, dict[int, list[float]]] = {k: defaultdict(list) for k in keys}
    for ts, d in samples:
        b = start + ((ts - start) // step) * step
        for k, v in d.items():
            acc[k][b].append(v)
    fn: Any = {"max": max, "min": min}.get(agg)
    for k in keys:
        pts = []
        for b in sorted(acc[k]):
            vals = acc[k][b]
            pts.append([b, round(fn(vals) if fn else sum(vals) / len(vals), 4)])
        out[k] = pts
    return out


def summarize(points: list[list[float]]) -> dict[str, float | None]:
    vals = [p[1] for p in points]
    if not vals:
        return {"min": None, "max": None, "avg": None, "last": None}
    return {"min": min(vals), "max": max(vals), "avg": round(sum(vals) / len(vals), 4), "last": vals[-1]}
