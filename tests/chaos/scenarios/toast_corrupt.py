#!/usr/bin/env python3
"""ROUND 2 - TOASTY!: a 'disk hiccup' eats the middle TOAST chunk of a few recent rows; the next report query trips over them.

PostgreSQL refuses plain DML on TOAST relations, so the damage is done the way the docs describe repairing it:
with the pg_surgery contrib module (heap_force_kill). The extension is dropped again afterwards.
If the database refuses to create the extension (an allowlist), the attacker reports the error and gives up.
"""
import os
import random
import subprocess
import sys

DB = os.environ.get("ARENA_DB", "ledger")
TABLE = os.environ.get("ARENA_TABLE", "public.entries")
KEY = os.environ.get("ARENA_KEY", "id")
COL = os.environ.get("ARENA_COL", "payload")
PICK = os.environ.get("ARENA_PICK", f"SELECT {KEY} FROM {TABLE} WHERE created_at < now() - interval '20 seconds' ORDER BY created_at DESC LIMIT 300")
VICTIMS = int(os.environ.get("ARENA_VICTIMS", "4"))


def psql(sql: str, check: bool = True) -> str:
    r = subprocess.run(["sudo", "-u", "postgres", "psql", "-X", "-At", "-d", DB, "-c", sql], capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"psql failed: {r.stderr.strip()}")
    return (r.stdout + r.stderr).strip()


candidates = psql(PICK).split()
victims = random.sample(candidates, min(VICTIMS, len(candidates)))
ids = ",".join(victims)
toast = psql(f"SELECT reltoastrelid::regclass FROM pg_class WHERE oid = '{TABLE}'::regclass")
chunks = psql(f"SELECT pg_column_toast_chunk_id({COL}) FROM {TABLE} WHERE {KEY} IN ({ids})").split()
tids = [psql(f"SELECT ctid FROM {toast} WHERE chunk_id = {c} AND chunk_seq = 1") for c in chunks]
print(f"toast relation {toast}; killing chunk_seq=1 of chunk_ids {chunks} at {tids}")
created = subprocess.run(["sudo", "-u", "postgres", "psql", "-X", "-At", "-d", DB, "-c", "CREATE EXTENSION IF NOT EXISTS pg_surgery"], capture_output=True, text=True)
if created.returncode != 0:
    err = next((ln.split(":", 1)[1].strip() for ln in created.stderr.splitlines() if ln.startswith(("ERROR", "FATAL"))), created.stderr.strip())
    print(f"ATTACK_BLOCKED: {err}")
    sys.exit(0)
array = ",".join(f"'{t}'::tid" for t in tids)
psql(f"SELECT heap_force_kill('{toast}'::regclass, ARRAY[{array}])")
psql("DROP EXTENSION pg_surgery")
print("report query:", psql(f"SELECT {KEY}, md5({COL}) FROM {TABLE} WHERE {KEY} IN ({ids})", check=False)[:300])
