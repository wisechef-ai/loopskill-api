"""Tests for Issue #17: LastUsedTracker — Redis-batched last_used_at.

Tests:
  - record() + drain() updates the DB row (in-memory path, no Redis)
  - Redis-missing falls back to in-memory dict
  - Multiple records for same key → max timestamp wins
  - drain() returns count of updated rows
  - drain() clears the cache after flush
  - Redis path: record() → drain() using a fake Redis hash
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.last_used_tracker import LastUsedTracker


# ── In-memory (no Redis) tests ────────────────────────────────────────────────

def test_record_and_drain_updates_db(db_session):
    """record() + drain() should bulk-UPDATE last_used_at in the DB."""
    from app.models import APIKey, User
    from uuid import uuid4 as _u4

    # Create a user + API key
    user = User(id=_u4(), email="tracker@test.com", display_name="Tracker User",
                created_at=datetime.now(timezone.utc))
    db_session.add(user)
    db_session.flush()

    key = APIKey(
        id=_u4(),
        key_prefix="rec_tracke",
        key_hash="dummyhash_tracker",
        user_id=user.id,
        name="tracker-key",
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(key)
    db_session.commit()

    tracker = LastUsedTracker(redis_client=None)
    ts = datetime.now(timezone.utc)
    tracker.record(key.id, ts)

    count = tracker.drain(db_session)
    assert count == 1

    db_session.expire(key)
    refreshed = db_session.query(APIKey).filter_by(id=key.id).first()
    assert refreshed.last_used_at is not None


def test_drain_clears_memory_cache():
    """After drain(), the in-memory cache is empty."""
    tracker = LastUsedTracker(redis_client=None)
    key_id = uuid4()
    tracker.record(key_id, datetime.now(timezone.utc))
    assert len(tracker._memory_cache) == 1

    # Mock db so drain doesn't need a real session
    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.update.return_value = 0
    tracker.drain(mock_db)
    assert len(tracker._memory_cache) == 0


def test_multiple_records_max_ts_wins():
    """Multiple record() calls for same key → latest timestamp stored."""
    tracker = LastUsedTracker(redis_client=None)
    key_id = uuid4()
    ts_old = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts_new = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    tracker.record(key_id, ts_old)
    tracker.record(key_id, ts_new)
    tracker.record(key_id, ts_old)  # old again — should not overwrite

    key_str = str(key_id)
    assert tracker._memory_cache[key_str] == ts_new


def test_drain_returns_zero_when_empty():
    """drain() on an empty tracker returns 0."""
    tracker = LastUsedTracker(redis_client=None)
    mock_db = MagicMock()
    result = tracker.drain(mock_db)
    assert result == 0


def test_drain_returns_update_count(db_session):
    """drain() returns the number of rows actually updated."""
    from app.models import APIKey, User

    user = User(id=uuid4(), email="drain2@test.com", display_name="Drain2 User",
                created_at=datetime.now(timezone.utc))
    db_session.add(user)
    db_session.flush()

    key1 = APIKey(id=uuid4(), key_prefix="rec_drain1", key_hash="dh_drain_1", user_id=user.id, name="k1",
                  is_active=True, created_at=datetime.now(timezone.utc))
    key2 = APIKey(id=uuid4(), key_prefix="rec_drain2", key_hash="dh_drain_2", user_id=user.id, name="k2",
                  is_active=True, created_at=datetime.now(timezone.utc))
    db_session.add_all([key1, key2])
    db_session.commit()

    tracker = LastUsedTracker(redis_client=None)
    tracker.record(key1.id)
    tracker.record(key2.id)

    count = tracker.drain(db_session)
    assert count == 2


# ── Redis fallback tests ──────────────────────────────────────────────────────

def test_redis_unavailable_falls_back_to_memory():
    """When Redis raises on hget, tracker records in _memory_cache."""
    fake_redis = MagicMock()
    fake_redis.hget.side_effect = Exception("Redis down")

    tracker = LastUsedTracker(redis_client=fake_redis)
    key_id = uuid4()
    tracker.record(key_id, datetime.now(timezone.utc))

    # Should be in memory cache
    assert str(key_id) in tracker._memory_cache


def test_redis_drain_path():
    """When Redis is available, drain uses hgetall + delete."""
    key_id = uuid4()
    ts = datetime.now(timezone.utc)

    fake_redis = MagicMock()
    fake_redis.hgetall.return_value = {str(key_id): ts.isoformat()}

    tracker = LastUsedTracker(redis_client=fake_redis)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.update.return_value = 1
    count = tracker.drain(mock_db)

    assert count == 1
    fake_redis.delete.assert_called_once()


def test_redis_drain_fallback_when_redis_fails_during_drain():
    """If Redis fails during drain, falls back to in-memory cache."""
    fake_redis = MagicMock()
    fake_redis.hgetall.side_effect = Exception("Redis down")

    tracker = LastUsedTracker(redis_client=fake_redis)
    key_id = uuid4()
    tracker._memory_cache[str(key_id)] = datetime.now(timezone.utc)

    mock_db = MagicMock()
    mock_db.query.return_value.filter.return_value.update.return_value = 1
    count = tracker.drain(mock_db)

    assert count == 1
    assert len(tracker._memory_cache) == 0
