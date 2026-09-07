"""Session-scoped PostgreSQL locks for work on one ARK.

The lock lives on the same database connection used to record the result.  A
worker that loses its connection cannot write a late completion, and PostgreSQL
releases the lock automatically when a worker process dies.
"""

from contextlib import contextmanager
from hashlib import blake2b
from typing import Iterator, Optional

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.database.connection import get_engine


def ark_lock_key(ark: str) -> int:
    """Return a stable signed bigint advisory-lock key for an ARK."""
    digest = blake2b(ark.encode("utf-8"), digest_size=8, person=b"dark-ark").digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


@contextmanager
def acquire_ark_lock(ark: str) -> Iterator[Optional[Session]]:
    """Yield a dedicated session when no other worker owns ``ark``.

    PostgreSQL session advisory locks are intentionally held across external
    I/O.  The session is also used for the final conditional database write,
    which makes ownership and completion one atomic responsibility.
    """
    connection = get_engine().connect()
    # A Session bound to an Engine returns its connection on commit. Bind to
    # this explicitly checked-out Connection instead, until unlock completes.
    db = Session(bind=connection, expire_on_commit=False)
    key = ark_lock_key(ark)
    acquired = False

    def reject_lost_connection(conn, *args):
        if conn.invalidated:
            raise RuntimeError("ARK lock connection was lost; abandon this attempt")

    event.listen(connection, "before_execute", reject_lost_connection)
    # Session autobegin happens before before_execute and can otherwise silently
    # reconnect an invalidated Connection. Reject it before the new transaction.
    event.listen(connection, "begin", reject_lost_connection)
    try:
        acquired = bool(
            db.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar()
        )
        db.commit()
        if not acquired:
            yield None
            return
        yield db
    finally:
        try:
            db.close()  # Roll back anything the caller did not explicitly commit.
            if acquired and not connection.invalidated:
                unlocked = connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                ).scalar()
                if not unlocked:
                    raise RuntimeError("ARK advisory lock ownership was lost")
                connection.commit()
        except Exception:
            connection.invalidate()  # Never return a possibly locked connection.
            raise
        finally:
            connection.close()
