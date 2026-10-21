"""Database engine and session helpers. SQLite (WAL) by default; PostgreSQL when the state should live with your other databases."""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from datetime import datetime, timezone
from typing import IO, Any, Callable, Iterator, TypeVar

from sqlalchemy import JSON, create_engine, event, make_url, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]

T = TypeVar("T")
log = logging.getLogger("raiden.console")
LOCK_KEY = 0x72414964  # "rAId": the PostgreSQL advisory lock a console holds on its state store


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


def utcnow() -> datetime:
    """Naive UTC: one representation for SQLite and PostgreSQL alike."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="seconds") + "Z"


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    # named in pg_stat_activity, unless the URL names it already
    args = {} if "application_name" in make_url(url).query else {"application_name": "raiden-console"}
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, connect_args=args)


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine = make_engine(url)
        self.Session = sessionmaker(self.engine, expire_on_commit=False)
        self.on_commit: Callable[[str, dict[str, Any]], None] | None = None

    def create_all(self) -> None:
        from . import models  # noqa: F401  (registers the tables)

        Base.metadata.create_all(self.engine)

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
        # side effects that must only happen once the change is stored (services.after_commit queues them)
        for kind, data in s.info.pop("after_commit", []):
            if self.on_commit is not None:
                self.on_commit(kind, data)

    def run(self, fn: Callable[[Session], T]) -> T:
        with self.session() as s:
            return fn(s)


class StoreInUse(RuntimeError):
    pass


class StoreLock:
    """One console per state store. A second one would collect the same servers, open the same incidents and act on them
    twice, so it refuses to start: a file lock next to a SQLite database, a session advisory lock on PostgreSQL. Either
    goes away with the process, however it ends."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.pg = db.engine.dialect.name == "postgresql"
        path = db.engine.url.database
        self.path = f"{path}.lock" if not self.pg and path and path != ":memory:" else None
        self._file: IO[str] | None = None
        self._conn: Connection | None = None

    def acquire(self) -> None:
        if self.pg:
            try:
                self._conn = self._pg_lock()
            except Exception as exc:  # noqa: BLE001  (a failing guard must not be what keeps the console down)
                log.warning("could not lock the state store (%s): make sure no other console uses it", exc)
                return
            if self._conn is None:
                raise StoreInUse(f"another rAIden Console is using this state store ({self._pg_holder()}): stop it first, one console per state store")
        elif self.path and fcntl is not None:
            f = open(self.path, "a+", encoding="utf-8")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                holder = f.read().strip() or "unknown"
                f.close()
                raise StoreInUse(f"another rAIden Console is using this state store ({holder}): stop it first, one console per state store") from None
            except OSError as exc:  # a file system without locks (some network file systems): carry on without one
                f.close()
                log.warning("cannot lock %s (%s): make sure no other console uses this state store", self.path, exc)
                return
            f.seek(0)
            f.truncate()
            f.write(f"pid {os.getpid()} on {socket.gethostname()} since {iso(utcnow())}\n")
            f.flush()
            self._file = f

    def check(self) -> bool:
        """Still ours? A PostgreSQL restart or failover ends the session that held the lock: take it again, unless
        another console took it in the meantime. False means this console must stop."""
        if not self.pg:
            return True
        if self._conn is not None:
            try:
                self._conn.execute(text("SELECT 1"))
                return True
            except Exception:  # noqa: BLE001
                self._close()
        try:
            self._conn = self._pg_lock()
        except Exception:  # noqa: BLE001  (the store is unreachable, so nobody can take it over: try again next time)
            return True
        return self._conn is not None

    def release(self) -> None:
        if self._file is not None:
            self._file.close()  # closing the file drops the lock
            self._file = None
        self._close()

    def _pg_lock(self) -> Connection | None:
        conn = self.db.engine.connect()
        try:
            conn.detach()  # closed for real on release: back in the pool, the connection would go on holding the lock
            conn.execution_options(isolation_level="AUTOCOMMIT")  # idle between checks, never idle in a transaction
            if conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
                return conn
        except Exception:
            conn.close()
            raise
        conn.close()
        return None

    def _pg_holder(self) -> str:
        try:
            with self.db.engine.connect() as c:
                row = c.execute(text(
                    "SELECT a.client_addr::text, a.backend_start FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                    "WHERE l.locktype = 'advisory' AND l.granted AND l.classid::bigint = 0 AND l.objid::bigint = :k AND l.objsubid = 1 "
                    "AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"), {"k": LOCK_KEY}).first()
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            return f"it holds advisory lock {LOCK_KEY}"
        return f"connected from {row[0] or 'a local socket'} since {iso(row[1])}"

    def _close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None
