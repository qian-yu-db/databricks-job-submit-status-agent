"""Turn tracking (observability): the /chat path opens a turn when a typed turn
starts and closes it once the request finishes. The scheduled sweeper job later
marks any turn still 'open' past its TTL as 'abandoned' (see test_sweeper.py)."""
from unittest.mock import MagicMock

from agent.status_store import InMemoryStatusStore


def test_inmemory_open_then_close():
    s = InMemoryStatusStore()
    s.open_turn("t1", "u@x.com:sess", "u@x.com")
    assert s._turns["t1"]["state"] == "open"
    s.close_turn("t1")
    assert s._turns["t1"]["state"] == "closed"


def test_inmemory_close_unknown_turn_is_noop():
    s = InMemoryStatusStore()
    s.close_turn("nope")            # must not raise
    assert "nope" not in s._turns


def test_inmemory_open_turn_records_created_at():
    """The in-memory turn shape matches the Postgres/schema shape (has created_at),
    so the sweeper's abandoned_turn_ids TTL logic can run against either."""
    s = InMemoryStatusStore()
    s.open_turn("t1", "th", "u")
    assert "created_at" in s._turns["t1"]


class _FakeCursor:
    def __init__(self, fail): self._fail = fail
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k):
        if self._fail:
            raise RuntimeError("db boom")


class _FakeConn:
    """Minimal psycopg-connection stand-in recording commit/rollback."""
    def __init__(self, fail): self._fail = fail; self.committed = False; self.rolled_back = False
    def cursor(self): return _FakeCursor(self._fail)
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True


def test_postgres_open_turn_best_effort_rolls_back_and_never_raises():
    """A failed turn INSERT must be rolled back (so the shared request connection is
    not left in an aborted-transaction state) and must not propagate."""
    from agent.status_store import PostgresStatusStore
    conn = _FakeConn(fail=True)
    PostgresStatusStore(conn).open_turn("t1", "th", "u")   # must NOT raise
    assert conn.rolled_back is True and conn.committed is False


def test_postgres_open_turn_commits_on_success():
    from agent.status_store import PostgresStatusStore
    conn = _FakeConn(fail=False)
    PostgresStatusStore(conn).open_turn("t1", "th", "u")
    assert conn.committed is True and conn.rolled_back is False


def test_postgres_close_turn_best_effort_rolls_back_and_never_raises():
    from agent.status_store import PostgresStatusStore
    conn = _FakeConn(fail=True)
    PostgresStatusStore(conn).close_turn("t1")             # must NOT raise
    assert conn.rolled_back is True and conn.committed is False


class _AbortAwareCursor:
    """Cursor that models Postgres aborted-transaction state: once the connection
    is aborted, every execute raises until the connection is rolled back."""
    def __init__(self, conn): self._conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, *a, **k):
        if self._conn.aborted:
            raise RuntimeError("current transaction is aborted (InFailedSqlTransaction)")
        self._conn.executed.append(sql)


class _AbortAwareConn:
    def __init__(self, start_aborted=False):
        self.aborted = start_aborted; self.executed = []; self.committed = False
    def cursor(self): return _AbortAwareCursor(self)
    def commit(self): self.committed = True
    def rollback(self): self.aborted = False        # rollback recovers the connection


def test_close_turn_recovers_aborted_connection_and_records_close():
    """If an earlier statement left the shared connection aborted, close_turn rolls
    back to recover it and still records the close — so a completed request's turn is
    never left 'open' for the sweeper to mislabel."""
    from agent.status_store import PostgresStatusStore
    conn = _AbortAwareConn(start_aborted=True)
    PostgresStatusStore(conn).close_turn("t1")
    assert conn.committed is True                       # the UPDATE ran and committed
    assert conn.executed and "UPDATE turns" in conn.executed[-1]
    # closes regardless of prior state (corrects a premature 'abandoned')
    assert "state='open'" not in conn.executed[-1]


def _wire_app(monkeypatch, route_and_run):
    import agent.app as app_mod
    fake_store = MagicMock()
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: fake_store)
    monkeypatch.setattr(app_mod, "lakebase_checkpointer", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(app_mod, "route_and_run", route_and_run)
    from fastapi.testclient import TestClient
    return app_mod, fake_store, TestClient(app_mod.app)


def test_chat_opens_and_closes_a_turn(monkeypatch):
    """A typed turn opens exactly one turn and closes it (same turn_id) once
    route_and_run returns."""
    _, fake_store, client = _wire_app(
        monkeypatch,
        lambda *a, **k: {"intent": "agent", "stage": "queued", "run_id": "a1",
                         "host": "web-prod-04", "status_lines": []})
    r = client.post("/chat", json={"message": "investigate web-prod-04", "session_id": "s1"})
    assert r.status_code == 200
    assert fake_store.open_turn.call_count == 1
    assert fake_store.close_turn.call_count == 1
    assert fake_store.open_turn.call_args[0][0] == fake_store.close_turn.call_args[0][0]


def test_chat_closes_turn_even_on_agent_error(monkeypatch):
    """A graceful agent error still 'finishes' the request → turn is closed. Only a
    crash that never returns leaves the turn open for the sweeper to abandon."""
    _, fake_store, client = _wire_app(
        monkeypatch,
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fm down")))
    r = client.post("/chat", json={"message": "hi", "session_id": "s1"})
    assert r.status_code == 200
    assert r.json()["intent"] == "error"
    assert fake_store.close_turn.call_count == 1


def test_background_poll_does_not_track_turns(monkeypatch):
    """The silent background status poll is not a user turn, so it must not open a
    turn (that would flood the turns table with poller pings)."""
    import agent.app as app_mod
    fake_store = MagicMock()
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: MagicMock())
    monkeypatch.setattr(app_mod, "PostgresStatusStore", lambda conn: fake_store)
    monkeypatch.setattr(app_mod, "check_status",
                        lambda *a, **k: {"stage": "running", "run_id": "a1",
                                         "host": "h", "status_lines": []})
    from fastapi.testclient import TestClient
    client = TestClient(app_mod.app)
    r = client.post("/chat", json={"message": "status", "run_id": "a1", "background": True})
    assert r.status_code == 200
    assert fake_store.open_turn.call_count == 0
