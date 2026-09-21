"""Short-term memory: thread_id composition + checkpointer threading."""
import agent.llm_agent as la


def test_compose_thread_id_with_session():
    assert la._compose_thread_id("u@x.com", "sess-1") == "u@x.com:sess-1"


def test_compose_thread_id_without_session_falls_back_to_user():
    assert la._compose_thread_id("u@x.com", "") == "u@x.com"
    assert la._compose_thread_id("u@x.com", None) == "u@x.com"


def test_build_agent_forwards_checkpointer(monkeypatch):
    """build_agent must pass checkpointer= through to create_agent."""
    import agent.app as app_mod
    monkeypatch.setattr(app_mod, "submit_investigation",
                        lambda *a, **k: {"run_id": "r", "host": "h", "stage": "queued",
                                         "status_lines": ["[start] ok"]})
    captured = {}
    import langchain.agents as la_agents
    real = la_agents.create_agent
    def spy(model, tools, **kw):
        captured["checkpointer"] = kw.get("checkpointer")
        return real(model, tools, **kw)
    monkeypatch.setattr(la_agents, "create_agent", spy)
    from unittest.mock import MagicMock
    from agent.status_store import InMemoryStatusStore
    from langgraph.checkpoint.memory import MemorySaver
    sentinel = MemorySaver()  # must be a BaseCheckpointSaver; bare object() rejected by langgraph 1.2.11
    la.build_agent("u@x.com", "t", InMemoryStatusStore(), model=MagicMock(), checkpointer=sentinel)
    assert captured["checkpointer"] is sentinel


def test_route_and_run_passes_thread_id_in_config(monkeypatch):
    """route_and_run must invoke with config configurable.thread_id."""
    from agent.status_store import InMemoryStatusStore
    seen = {}
    class _Msg:
        content = "ok"
    class _FakeAgent:
        def invoke(self, state, config=None):
            seen["config"] = config
            return {"messages": [_Msg()]}
    monkeypatch.setattr(la, "build_agent", lambda *a, **k: (_FakeAgent(), []))
    la.route_and_run("hi", "", "web-prod-04", "u@x.com", "t",
                     InMemoryStatusStore(), thread_id="u@x.com:sess-1")
    assert seen["config"]["configurable"]["thread_id"] == "u@x.com:sess-1"
    assert seen["config"]["recursion_limit"] == 6


# --- End-to-end threading proof: a real MemorySaver + a fake tool-capable model.
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration

_SEEN: list[int] = []


class _FakeToolModel(BaseChatModel):
    """Minimal chat model for create_agent: supports bind_tools and returns
    a plain AIMessage (no tool calls, so the ReAct loop ends immediately). Records
    how many messages it was given each call so we can prove history threading."""
    def bind_tools(self, tools, **kw):
        return self
    def _generate(self, messages, stop=None, run_manager=None, **kw):
        _SEEN.append(len(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])
    @property
    def _llm_type(self):
        return "fake-tool"


def test_checkpointer_threads_history_across_turns():
    """Same thread_id: turn 2 sees turn 1's persisted messages. A different
    thread_id starts fresh. Uses a real MemorySaver (no live LLM)."""
    from langgraph.checkpoint.memory import MemorySaver
    from agent.status_store import InMemoryStatusStore
    store = InMemoryStatusStore()
    saver = MemorySaver()

    _SEEN.clear()
    la.route_and_run("hello", "", "web-prod-04", "u@x.com", "t", store,
                     model=_FakeToolModel(), checkpointer=saver, thread_id="u@x.com:A")
    first = _SEEN[-1]
    la.route_and_run("again", "", "web-prod-04", "u@x.com", "t", store,
                     model=_FakeToolModel(), checkpointer=saver, thread_id="u@x.com:A")
    second = _SEEN[-1]
    assert second > first, "turn 2 on the same thread must see turn 1's history"

    _SEEN.clear()
    la.route_and_run("fresh", "", "web-prod-04", "u@x.com", "t", store,
                     model=_FakeToolModel(), checkpointer=saver, thread_id="u@x.com:B")
    assert _SEEN[-1] == first, "a different thread_id must not inherit history"
