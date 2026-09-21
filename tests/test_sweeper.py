from datetime import datetime, timezone, timedelta
from jobs.sweeper_job import abandoned_turn_ids

def test_marks_only_stale_open_turns():
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    turns = [
        {"turn_id": "a", "state": "open",   "created_at": now - timedelta(seconds=3600)},
        {"turn_id": "b", "state": "open",   "created_at": now - timedelta(seconds=10)},
        {"turn_id": "c", "state": "closed", "created_at": now - timedelta(seconds=9999)},
    ]
    assert abandoned_turn_ids(turns, now=now, ttl_seconds=900) == ["a"]
