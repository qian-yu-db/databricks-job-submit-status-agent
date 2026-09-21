"""
Unit tests for agent.llm_agent.

Tests exercise the module-level _impl functions directly (bypassing the LLM)
via monkeypatched agent.app symbols.  Full LLM routing is validated by the
eval harness in Task 9 — not here.
"""

import agent.llm_agent as la
from agent.status_store import InMemoryStatusStore


def test_investigate_impl_carries_identity_and_collects_output(monkeypatch):
    import agent.app as app_mod

    seen = {}
    monkeypatch.setattr(
        app_mod,
        "submit_investigation",
        lambda host, uid, tok, store, **kw: seen.update(host=host, uid=uid, tok=tok)
        or {"run_id": "r9", "host": host, "stage": "queued",
            "status_lines": ["[start] ..."]},
    )

    store = InMemoryStatusStore()
    outputs: list = []
    result = la._investigate_impl("web-prod-04", "investigation", "ryan@bw.com", "tok-abc", store, outputs)

    assert seen["uid"] == "ryan@bw.com"
    assert seen["tok"] == "tok-abc"
    assert seen["host"] == "web-prod-04"
    assert outputs and outputs[-1]["run_id"] == "r9"
    assert "[start]" in result


def test_check_status_impl_carries_identity_and_collects_output(monkeypatch):
    import agent.app as app_mod

    seen = {}
    monkeypatch.setattr(
        app_mod,
        "check_status",
        lambda run_id, host, uid, store: seen.update(run_id=run_id, uid=uid)
        or {"run_id": run_id, "host": host, "stage": "running",
            "status_lines": ["[status] r9: running"]},
    )

    store = InMemoryStatusStore()
    outputs: list = []
    result = la._check_status_impl("r9", "ryan@bw.com", store, outputs)

    assert seen["uid"] == "ryan@bw.com"
    assert seen["run_id"] == "r9"
    assert outputs and outputs[-1]["stage"] == "running"
    assert "[status]" in result


def test_diagnose_impl_carries_run_id_and_collects_output(monkeypatch):
    import agent.app as app_mod

    seen = {}
    monkeypatch.setattr(
        app_mod,
        "check_status",
        lambda run_id, host, uid, store: seen.update(run_id=run_id, uid=uid)
        or {"run_id": run_id, "host": host, "stage": "failed",
            "status_lines": ["[status] r9: failed", "[diagnosis] disk full"]},
    )

    store = InMemoryStatusStore()
    outputs: list = []
    result = la._diagnose_impl("r9", "ryan@bw.com", store, outputs)

    assert seen["uid"] == "ryan@bw.com"
    assert seen["run_id"] == "r9"
    assert outputs and outputs[-1]["stage"] == "failed"
    assert "[diagnosis]" in result


def test_flatten_content_string_passthrough():
    assert la._flatten_content("hello world") == "hello world"


def test_flatten_content_reasoning_model_blocks():
    """claude-sonnet-5 returns content as a list of blocks (reasoning + text).
    _flatten_content must return only the human-readable text, never the raw
    JSON of the block list (the UI was dumping that verbatim)."""
    content = [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "", "signature": "abc"}]},
        {"type": "text", "text": "I don't have an active run_id to check yet."},
    ]
    out = la._flatten_content(content)
    assert out == "I don't have an active run_id to check yet."
    assert "reasoning" not in out and "signature" not in out


def test_flatten_content_multiple_text_blocks_joined():
    content = [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}]
    assert la._flatten_content(content) == "line one\nline two"


def test_flatten_content_json_string_block_list():
    """A checkpointer round-trip can return .content as a JSON STRING encoding the
    block list; _flatten_content must parse it and return only the text."""
    import json as _json
    content = _json.dumps([
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "", "signature": "s"}]},
        {"type": "text", "text": "Run 455015e4 for web-prod-04 is still queued."},
    ])
    assert isinstance(content, str)
    out = la._flatten_content(content)
    assert out == "Run 455015e4 for web-prod-04 is still queued."
    assert '"type"' not in out and "reasoning" not in out


def test_flatten_content_plain_string_starting_with_bracket_unchanged():
    """A non-JSON string that merely starts with '[' is returned unchanged."""
    assert la._flatten_content("[status] run 455015e4: queued") == "[status] run 455015e4: queued"


def test_flatten_content_json_array_without_text_blocks_kept_intact():
    """A JSON array that parses but has no text block must NOT be dropped to '' —
    the original string is returned (guards against silently losing a reply)."""
    s = '[{"type": "image", "url": "x.png"}]'
    assert la._flatten_content(s) == s


def test_route_and_run_prefers_queued_output(monkeypatch):
    """A turn that both launches a run and checks status must return the QUEUED
    launch (so the client polls the new run), not the trailing status output."""
    class _Msg:
        content = "done"

    class _FakeAgent:
        def __init__(self, outputs):
            self._o = outputs
        def invoke(self, state, config=None):
            self._o.append({"run_id": "NEW", "host": "web-prod-04", "stage": "queued",
                            "status_lines": ["[queued] run NEW"]})
            self._o.append({"run_id": "NEW", "host": "web-prod-04", "stage": "unknown",
                            "status_lines": ["[status] no run found"]})
            return {"messages": [_Msg()]}

    from agent.status_store import InMemoryStatusStore
    outputs: list = []
    monkeypatch.setattr(la, "build_agent", lambda *a, **k: (_FakeAgent(outputs), outputs))
    resp = la.route_and_run("investigate web-prod-04 and give status", "", "web-prod-04",
                            "u@x.com", "t", InMemoryStatusStore())
    assert resp["stage"] == "queued" and resp["run_id"] == "NEW"


def test_route_and_run_persists_only_user_message(monkeypatch):
    """Only the user message is sent into the graph (the host hint rides in the
    prompt), so no ("system", ctx) message accumulates in the checkpointed thread."""
    seen = {}

    class _Msg:
        content = "ok"

    class _FakeAgent:
        def invoke(self, state, config=None):
            seen["messages"] = state["messages"]
            return {"messages": [_Msg()]}

    monkeypatch.setattr(la, "build_agent", lambda *a, **k: (_FakeAgent(), []))
    la.route_and_run("hi", "", "web-prod-04", "u@x.com", "t", InMemoryStatusStore())
    assert [m[0] for m in seen["messages"]] == ["user"]


def test_build_agent_folds_host_hint_into_prompt(monkeypatch):
    """The current host is added to the per-invoke prompt (not a stored message)."""
    import agent.app as app_mod
    import langchain.agents as la_agents
    from unittest.mock import MagicMock
    monkeypatch.setattr(app_mod, "submit_investigation",
                        lambda *a, **k: {"run_id": "r", "host": "h", "stage": "queued",
                                         "status_lines": ["ok"]})
    captured = {}
    real = la_agents.create_agent
    monkeypatch.setattr(la_agents, "create_agent",
                        lambda m, t, **kw: captured.setdefault("prompt", kw.get("system_prompt")) or real(m, t, **kw))
    la.build_agent("u@x.com", "t", InMemoryStatusStore(), model=MagicMock(), host="db-replica-07")
    assert "db-replica-07" in (captured["prompt"] or "")


def test_route_and_run_empty_reply_uses_fallback(monkeypatch):
    """A reply with no text (e.g. only reasoning blocks) yields the fallback line,
    never an empty status_lines / blank bubble."""
    class _Msg:
        content = ""

    class _FakeAgent:
        def invoke(self, state, config=None):
            return {"messages": [_Msg()]}

    monkeypatch.setattr(la, "build_agent", lambda *a, **k: (_FakeAgent(), []))
    resp = la.route_and_run("hello", "", "web-prod-04", "u@x.com", "t", InMemoryStatusStore())
    assert resp["status_lines"] == [la._EMPTY_REPLY]


def test_investigate_impl_returns_full_run_id_to_model(monkeypatch):
    """The model-facing return must carry the FULL run_id (so a later turn can
    call check_status), while UI status_lines stay truncated/pretty."""
    import agent.app as app_mod
    monkeypatch.setattr(
        app_mod, "submit_investigation",
        lambda host, uid, tok, store, **kw: {
            "run_id": "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb", "host": host,
            "stage": "queued",
            "status_lines": [f"[queued] run abcdef12 started on serverless"]},
    )
    outputs = []
    out = la._investigate_impl("web-prod-04", "investigation", "u@x.com", "t", InMemoryStatusStore(), outputs)
    assert "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb" in out          # full uuid to model
    assert "web-prod-04" in out
    # UI status_lines remain truncated (unchanged)
    assert outputs[-1]["status_lines"] == ["[queued] run abcdef12 started on serverless"]


def test_build_agent_returns_agent_and_outputs_list(monkeypatch):
    """build_agent with an injected stub model returns (agent, list) without
    hitting the real endpoint."""
    import agent.app as app_mod
    from unittest.mock import MagicMock

    # Patch submit_investigation so the tool closure doesn't need a real store.
    monkeypatch.setattr(
        app_mod,
        "submit_investigation",
        lambda host, uid, tok, store, **kw: {"run_id": "rx", "host": host,
                                              "stage": "queued",
                                              "status_lines": ["[start] ok"]},
    )

    stub_model = MagicMock()
    store = InMemoryStatusStore()
    agent_obj, outputs = la.build_agent("u@x.com", "t", store, model=stub_model)

    assert hasattr(agent_obj, "invoke"), "build_agent must return an invokable agent"
    assert isinstance(outputs, list), "build_agent must return a mutable outputs list"


def test_build_agent_prompt_includes_catalog(monkeypatch):
    import agent.app as app_mod
    import agent.job_registry as jr
    monkeypatch.setattr(app_mod, "submit_investigation",
                        lambda *a, **k: {"run_id": "r", "host": "h", "stage": "queued",
                                         "status_lines": ["[start] ok"]})
    monkeypatch.setattr(jr, "get_registry",
                        lambda force=False: {"failure_demo": jr.JobSpec(
                            "failure_demo", "F", "Run the failing demo", 1, {}, {"host": "fail-demo-01"})})
    captured = {}
    import langchain.agents as la_agents
    real = la_agents.create_agent
    monkeypatch.setattr(la_agents, "create_agent",
                        lambda m, t, **kw: captured.setdefault("prompt", kw.get("system_prompt")) or real(m, t, **kw))
    from unittest.mock import MagicMock
    from agent.status_store import InMemoryStatusStore
    la.build_agent("u@x.com", "t", InMemoryStatusStore(), model=MagicMock())
    assert "failure_demo" in (captured["prompt"] or "")
