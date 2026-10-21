"""The one-console lock on a PostgreSQL state store, against a real server (on the lab VM, the console's state store):

    python tests/live/check_store_lock.py postgresql://raiden:<password>@127.0.0.1:5499/raiden

While one console holds the store, a second is refused and told where the first connected from; when the holder's
session is cut, as a restart of the store would, its next check takes the lock back; once it lets go, the next console
starts. The SQLite variant is covered by tests/test_console_api.py.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from raiden.console.db import LOCK_KEY, Database, StoreInUse, StoreLock


def main(url: str) -> int:
    first, second = StoreLock(Database(url)), StoreLock(Database(url))
    first.acquire()
    try:
        second.acquire()
        print("FAIL: a second console got the lock")
        return 1
    except StoreInUse as exc:
        if "connected from" not in str(exc):
            print(f"FAIL: the refusal does not say who holds the store: {exc}")
            return 1
        print(f"ok   a second console is refused: {exc}")
    with Database(url).engine.connect() as c:
        cut = c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_locks WHERE locktype = 'advisory' AND objid::bigint = :k"),
                        {"k": LOCK_KEY}).scalars().all()
    if cut != [True] or not first.check():
        print(f"FAIL: the holder did not take the lock back after its session was cut ({cut})")
        return 1
    print("ok   the holder takes the lock back after its session is cut")
    first.release()
    second.acquire()
    second.release()
    print("ok   the next console starts once the first has stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
