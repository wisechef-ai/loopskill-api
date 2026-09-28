"""A short-lived session that commits independently of the caller's request.

paywall_0925. Telemetry written at a tier gate is written immediately before
the handler RAISES (HTTPException / CookbookInstallError). The request session
is then closed without a commit, so a row added to it is silently lost — which
is exactly how a paywall hit would stay invisible. Writing through the caller's
session and committing it is worse: it would flush whatever else the handler
had pending.

``side_session(db)`` opens a separate Session on the caller's *bind*:

* production — the bind is the Engine, so the side session gets its own pooled
  connection and its commit is independent of the request transaction;
* tests — the bind is the per-test Connection that already holds the outer
  rollback transaction; SQLAlchemy 2.0's default ``join_transaction_mode``
  ("conservative_savepoint") joins it WITHOUT committing it, so rows land in
  the test's transaction, are visible to the test's own session, and are
  rolled back with it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session


@contextmanager
def side_session(db: Session) -> Iterator[Session]:
    """Yield a Session on ``db``'s bind; commit on success, roll back on error."""
    side = Session(bind=db.get_bind(), autoflush=False)
    try:
        yield side
        side.commit()
    except BaseException:
        side.rollback()
        raise
    finally:
        side.close()
