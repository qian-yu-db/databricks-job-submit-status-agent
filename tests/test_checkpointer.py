"""Checkpointer factory: constructs a PostgresSaver over a fresh conn and runs
setup exactly once per process."""
import agent.app as app_mod
from unittest.mock import MagicMock


def test_lakebase_checkpointer_builds_saver_and_setups_once(monkeypatch):
    # Reset the process guard.
    monkeypatch.setattr(app_mod, "_CHECKPOINTER_READY", False, raising=False)
    made_conns = []
    monkeypatch.setattr(app_mod, "lakebase_conn",
                        lambda **kw: made_conns.append(kw) or MagicMock())
    saver = MagicMock()
    created = {"n": 0}
    def _fake_saver(conn):
        created["n"] += 1
        return saver
    monkeypatch.setattr(app_mod, "PostgresSaver", _fake_saver, raising=False)

    s1, c1 = app_mod.lakebase_checkpointer()
    s2, c2 = app_mod.lakebase_checkpointer()
    assert s1 is saver and s2 is saver
    assert saver.setup.call_count == 1            # setup runs once, not per call
    assert created["n"] == 2                       # a fresh saver/conn per call
    # checkpointer connection asks for autocommit + dict_row
    assert made_conns[0].get("autocommit") is True


def test_lakebase_checkpointer_closes_conn_on_setup_failure(monkeypatch):
    """Connection must be closed when PostgresSaver.setup() raises (leak prevention)."""
    monkeypatch.setattr(app_mod, "_CHECKPOINTER_READY", False, raising=False)

    mock_conn = MagicMock()
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: mock_conn)

    fake_saver = MagicMock()
    fake_saver.setup.side_effect = RuntimeError("setup boom")

    monkeypatch.setattr(app_mod, "PostgresSaver", lambda conn: fake_saver, raising=False)

    import pytest
    with pytest.raises(RuntimeError, match="setup boom"):
        app_mod.lakebase_checkpointer()

    mock_conn.close.assert_called_once()


def test_lakebase_checkpointer_uses_dedicated_schema(monkeypatch):
    """The factory creates + uses an SP-owned schema (search_path) so it doesn't
    collide with pre-existing public.checkpoint* tables owned by another role."""
    monkeypatch.setattr(app_mod, "_CHECKPOINTER_READY", False, raising=False)
    executed = []
    cur = MagicMock()
    cur.execute.side_effect = lambda sql, *a: executed.append(sql)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    monkeypatch.setattr(app_mod, "lakebase_conn", lambda **kw: conn)
    monkeypatch.setattr(app_mod, "PostgresSaver", lambda c: MagicMock(), raising=False)

    app_mod.lakebase_checkpointer()

    joined = " | ".join(executed)
    assert "CREATE SCHEMA IF NOT EXISTS agent_memory" in joined
    assert "SET search_path TO agent_memory" in joined
