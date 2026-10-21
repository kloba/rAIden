"""How far one console goes on its default SQLite metric store. Not a test: run it on the hardware you plan to use.

    python -m tests.bench_metric_store [servers] [metrics]          # defaults: 300 servers, 80 metrics each

It measures one collection round of every server, the 15-minute roll-ups, the chart reads the UI makes and the bytes a
server costs on disk, and works out the store size at the default retention (raw samples 3 days, roll-ups 93 days, one
collection a minute). docs/production-readiness.md has the results of one run.
"""

from __future__ import annotations

import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from raiden.console.db import Database
from raiden.console.models import Server
from raiden.console.timeseries import ROLLUP_S, MetricStore

SERVERS = int(sys.argv[1]) if len(sys.argv) > 1 else 300
METRICS = int(sys.argv[2]) if len(sys.argv) > 2 else 80
EVERY, MIN_EVERY, RAW_DAYS, ROLLUP_DAYS = 60, 10, 3, 93


def values() -> dict[str, float]:
    return {k: random.random() * 100 for k in keys}


def size(path: Path) -> int:
    with sqlite3.connect(path) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return path.stat().st_size


tmp = Path(tempfile.mkdtemp())
path = tmp / "bench.db"
db = Database(f"sqlite:///{path}")
db.create_all()
with db.session() as s:
    s.add_all(Server(id=f"pg{i:03d}", name=f"pg{i:03d}", host="db", info={}) for i in range(SERVERS))
store = MetricStore(db)
keys = [f"m{j}" for j in range(METRICS - 10)] + [f"tps[db{j}]" for j in range(10)]
now = 1_790_000_000 - 1_790_000_000 % ROLLUP_S

# one collection round: every server stores one snapshot, as the collectors do
rounds = []
for r in range(5):
    t0 = time.perf_counter()
    for i in range(SERVERS):
        store.append(f"pg{i:03d}", now + r * EVERY, values())
    rounds.append(time.perf_counter() - t0)
per_round = statistics.median(rounds)
print(f"collection round, {SERVERS} servers x {METRICS} metrics: {per_round * 1000:.0f} ms median "
      f"({per_round / MIN_EVERY * 100:.1f}% of the shortest interval a server may have, {MIN_EVERY} s)")

# a week of one server at one collection a minute: roll-ups, chart reads and bytes on disk
before = size(path)
week = [(now - 7 * 86400 + k * EVERY, values()) for k in range(7 * 86400 // EVERY)]
store.append_many("pg000", week)
raw_bytes = (size(path) - before) / len(week)
before = size(path)
t0 = time.perf_counter()
made = store.rollup(now)
print(f"roll-ups of a week of one server ({len(week):,} samples into {made:,} buckets): {(time.perf_counter() - t0) * 1000:.0f} ms")
rollup_bytes = (size(path) - before) / max(1, made)  # the rounds above are newer than the last finished bucket
for label, span in (("1 h", 3600), ("24 h", 86400), ("7 d", 7 * 86400)):
    lat = []
    for _ in range(5):
        t0 = time.perf_counter()
        store.series("pg000", keys[:6], now - span, now, max_points=240)
        lat.append(time.perf_counter() - t0)
    print(f"chart read, 6 metrics over {label}: {statistics.median(lat) * 1000:.1f} ms median")
t0 = time.perf_counter()
for i in range(SERVERS):
    store.latest_values(f"pg{i:03d}")
print(f"latest values of all {SERVERS} servers (the estate page): {(time.perf_counter() - t0) * 1000:.1f} ms")

per_server = RAW_DAYS * 86400 / EVERY * raw_bytes + ROLLUP_DAYS * 86400 / ROLLUP_S * rollup_bytes
print(f"on disk: {raw_bytes:,.0f} bytes a sample, {rollup_bytes:,.0f} bytes a 15-minute roll-up; at the default retention "
      f"{per_server / 1e6:.0f} MB a server, {per_server * SERVERS / 1e9:.1f} GB for {SERVERS} servers")
