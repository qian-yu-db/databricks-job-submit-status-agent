"""
LangGraph ReAct agent — routes chat messages to governed investigation tools.

Circular-import avoidance: agent/app.py imports agent.llm_agent (for get_model),
so this module must never import agent.app at module load.  All imports of
agent.app symbols are done lazily inside the _impl function bodies (call time).
"""

from __future__ import annotations

import json
from functools import lru_cache

_SYSTEM = (
    "You are a cybersecurity investigation orchestrator for a security team. "
    "You have exactly three tools: investigate(host) to start a suspicious-login "
    "investigation, check_status(run_id) to report an in-flight run, and "
    "diagnose_run(run_id) to explain a FAILED run. Choose the single best tool for "
    "the user's message and pass concise args. If the user asks for something outside "
    "investigating a host, checking a run, or diagnosing a failure, do NOT call a tool "
    "— briefly say you can investigate a host or report/diagnose a run. Never invent "
    "hostnames or run ids; use only what the user or the provided context gives you."
)

# Shown when the model returns no text (e.g. only reasoning blocks) so the UI
# never renders a blank agent bubble.
_EMPTY_REPLY = (
    "I don't have a response for that — try investigating a host "
    "(e.g. \"investigate web-prod-04\") or asking for a run's status."
)


def _compose_thread_id(user_id: str, session_id) -> str:
    """One memory thread per conversation, scoped per OBO user so threads are
    never shared across users. Falls back to the bare user id when the client
    sends no session id (older client / direct curl)."""
    return f"{user_id}:{session_id}" if session_id else user_id


@lru_cache(maxsize=1)
def get_model():
    from databricks_langchain import ChatDatabricks
    # Route through Unity AI Gateway: the model is referenced as its governed
    # Unity Catalog model service (system.ai.claude-sonnet-5) and use_ai_gateway
    # sends the call through the gateway, so the gateway's configured controls
    # (usage tracking, payload logging, guardrails, rate limits) apply to the call.
    # The app SP needs UC EXECUTE on the model service.
    # No temperature: claude-sonnet-5 is a reasoning model and rejects the
    # temperature parameter ("does not support the temperature parameter").
    return ChatDatabricks(model="system.ai.claude-sonnet-5", use_ai_gateway=True)


# ---------------------------------------------------------------------------
# Module-level tool bodies — testable without the LLM; @tool wrappers call these.
# ---------------------------------------------------------------------------

def _investigate_impl(host, job_key, user_id, user_token, store, outputs):
    from agent.app import submit_investigation
    resp = submit_investigation(host, user_id, user_token, store, job_key=job_key)
    outputs.append(resp)
    text = "\n".join(resp["status_lines"])
    if resp.get("stage") == "queued" and resp.get("run_id"):
        text += f"\n(run_id: {resp['run_id']} for host {resp.get('host')})"
    return text


def _catalog_text() -> str:
    """Render the enabled workflow catalog for the system prompt (best-effort; the
    server still validates job_key). Empty string if the registry can't be read."""
    try:
        from agent.job_registry import get_registry
        reg = get_registry()
    except Exception:
        return ""
    if not reg:
        return ""
    lines = ["", "Available workflows — call investigate(host, job_key):"]
    for key, spec in reg.items():
        req = ", ".join(n for n, d in (spec.param_schema or {}).items() if d.get("required"))
        lines.append(f"- {key}: {spec.description}" + (f" (needs: {req})" if req else ""))
    return "\n".join(lines)


def _check_status_impl(run_id, user_id, store, outputs):
    from agent.app import check_status
    resp = check_status(run_id, None, user_id, store)
    outputs.append(resp)
    return "\n".join(resp["status_lines"])


def _diagnose_impl(run_id, user_id, store, outputs):
    # check_status already produces and caches the diagnosis on failure
    from agent.app import check_status
    resp = check_status(run_id, None, user_id, store)
    outputs.append(resp)
    return "\n".join(resp["status_lines"])


# ---------------------------------------------------------------------------
# Agent builder
# ---------------------------------------------------------------------------

def build_agent(user_id, user_token, store, *, model=None, checkpointer=None, host=None):
    """Build a ReAct agent with three governed tool closures.

    Returns (agent, outputs_list).  The outputs_list is mutated in-place by
    each tool call so the caller can inspect structured results after invoke().

    The `model` param allows tests to inject a stub without hitting the endpoint.
    The `checkpointer` param is forwarded to create_agent for memory persistence.
    The `host` hint (current host) is folded into the per-invoke prompt rather than
    a stored message, so it never accumulates in the checkpointed thread.
    """
    from langchain.agents import create_agent
    from langchain_core.tools import tool

    outputs: list[dict] = []

    @tool
    def investigate(host: str = "", job_key: str = "investigation") -> str:
        """Start an investigation workflow. `job_key` selects the workflow from the
        catalog in the system prompt (default 'investigation'); `host` is the target."""
        return _investigate_impl(host, job_key, user_id, user_token, store, outputs)

    @tool
    def check_status(run_id: str) -> str:
        """Report the status of an in-flight investigation run by its run_id."""
        return _check_status_impl(run_id, user_id, store, outputs)

    @tool
    def diagnose_run(run_id: str) -> str:
        """Explain why a FAILED investigation run failed and recommend a fix."""
        return _diagnose_impl(run_id, user_id, store, outputs)

    # system_prompt is applied per-invoke (not persisted to the thread), so the
    # host hint here never accumulates in memory across turns. (create_agent is
    # LangChain 1.x's successor to the deprecated langgraph create_react_agent;
    # `prompt=` was renamed to `system_prompt=`.)
    host_hint = f"\nThe user is currently working with host: {host}." if host else ""
    agent = create_agent(
        model or get_model(),
        [investigate, check_status, diagnose_run],
        system_prompt=_SYSTEM + _catalog_text() + host_hint,
        checkpointer=checkpointer,
    )
    return agent, outputs


# ---------------------------------------------------------------------------
# Message-content flattening
# ---------------------------------------------------------------------------

def _flatten_content(content) -> str:
    """Reduce a LangChain message's `.content` to human-readable text.

    Reasoning models (claude-sonnet-5) return `.content` as a LIST of blocks —
    e.g. [{"type":"reasoning",...}, {"type":"text","text":"..."}] — not a string.
    We keep only the text blocks (dropping reasoning/summary blocks) so the UI
    never renders the raw block JSON.  A plain string passes through unchanged.
    """
    if isinstance(content, str):
        # A checkpointer round-trip can hand the reasoning model's content back
        # as a JSON-serialized block list (a string like '[{"type":"reasoning"..
        # },{"type":"text","text":"..."}]'); parse and flatten it. A normal text
        # reply (or any non-block string) is returned unchanged.
        s = content.strip()
        if s.startswith("[") and '"type"' in s:
            try:
                parsed = json.loads(s)
            except ValueError:
                return content
            # Only flatten when it really is a content-block list; otherwise a
            # normal reply that happens to be a JSON array is returned intact,
            # and a flatten that yields nothing falls back to the original.
            if isinstance(parsed, list) and any(
                isinstance(b, dict) and "type" in b for b in parsed
            ):
                flat = _flatten_content(parsed)
                return flat if flat else content
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)
    return str(content or "")


# ---------------------------------------------------------------------------
# Top-level route-and-run (called by app.py /chat; exercised by eval harness)
# ---------------------------------------------------------------------------

def route_and_run(message, run_id, host, user_id, user_token, store,
                  *, model=None, checkpointer=None, thread_id=None):
    """Invoke the ReAct agent and return a structured response dict.

    Returns: {status_lines, incident, stage, run_id, host, intent, agent_message}
    Raises on model/endpoint failure; the caller returns a graceful error —
    there is no deterministic fallback (the LLM agent is the only mode).

    The `checkpointer` and `thread_id` params enable short-term memory: pass a
    MemorySaver (or any BaseCheckpointSaver) and a per-session thread_id to
    persist conversation history across turns.
    """
    agent, outputs = build_agent(user_id, user_token, store, model=model,
                                 checkpointer=checkpointer, host=host)
    # Only the user's message is persisted to the thread; the host hint rides in
    # the prompt (build_agent), so stale context never accumulates in memory.
    result = agent.invoke(
        {"messages": [("user", message)]},
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 6},
    )
    raw = result["messages"][-1].content if result.get("messages") else ""
    final = _flatten_content(raw)
    # Prefer a queued launch when a turn both starts a run AND checks status
    # (e.g. "investigate X and tell me its status"), so the client polls the
    # newly-launched run rather than the trailing status output.
    structured = {}
    if outputs:
        queued = [o for o in outputs if o.get("stage") == "queued"]
        structured = queued[-1] if queued else outputs[-1]
    return {
        "run_id": structured.get("run_id", run_id),
        "host": structured.get("host", host),
        "stage": structured.get("stage", "help"),
        "incident": structured.get("incident"),
        # Forward the HITL fields so a typed status check (not just the poller)
        # can surface the Approve/Decline gate.
        "awaiting_approval": structured.get("awaiting_approval"),
        "proposal": structured.get("proposal"),
        "status_lines": structured.get("status_lines", [final] if final else [_EMPTY_REPLY]),
        "agent_message": final,
        "intent": "agent",
    }
