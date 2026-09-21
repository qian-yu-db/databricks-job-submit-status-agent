# 05 · Tests & Eval

**Files:** `tests/` (the offline suite), `eval/routing_eval.py` (the real-model eval)

The project has two very different kinds of checks: a **fast, fully offline unit
suite** that runs on every change, and a **single real-model eval** you run
deliberately. This section explains both, and — just as usefully — shows how the
codebase's design choices are what make the offline suite possible.

## The offline suite

Run it with:

```bash
uv run pytest -q                                  # ~97 tests, a few seconds
uv run pytest tests/test_llm_agent.py::test_name -q   # a single test
```

**It needs no workspace.** It monkeypatches the three things that would otherwise
require Databricks — **Lakebase, the LLM, and the Jobs API** — so it runs anywhere
in seconds. The mocking leans on design choices made throughout the codebase:

| To avoid… | The test uses… | Made possible by… |
|-----------|----------------|-------------------|
| A real Postgres | `InMemoryStatusStore` | the `StatusStore` **Protocol** with two impls ([state & memory](02-llm-agent.md)) |
| The real model | a `MagicMock` / a tiny fake `BaseChatModel` | `build_agent(model=…)` injection ([the LLM agent](02-llm-agent.md)) |
| Launching real jobs | monkeypatched `submit_investigation` / `check_status` | the module-level `_impl` tool bodies ([the LLM agent](02-llm-agent.md)) |
| A SQL warehouse | a fake `execute_sql` returning canned rows | `execute_sql` **dependency injection** ([Setup, Part B](01-setup.md)) |
| The MCP app | the in-process `StubBackend` | the stub/wire split ([Identity & passthrough](04-identity-and-passthrough.md)) |

So the same properties that make the app modular — the Protocol, the injectable
model and SQL executor, the stub backend — are exactly what make it testable
offline. Testability here is a *consequence* of the architecture, not a separate
effort.

### What's covered, by area

The suite maps cleanly onto the sections:

| Area | Test files |
|------|------------|
| LLM agent, memory, routing | `test_llm_agent`, `test_agent_memory`, `test_checkpointer`, `test_chat_agent_integration`, `test_async_chat` |
| Job registry | `test_job_registry` |
| Jobs & investigation | `test_investigation`, `test_stages`, `test_fail_demo`, `test_launch_capture` |
| State & store | `test_status_store`, `test_status_store_jobrunid`, `test_failure_detection`, `test_models` |
| Failure diagnosis | `test_diagnosis` |
| Identity / OBO | `test_obo_sql`, `test_graph` |
| ServiceNow MCP | `test_mcp_wire`, `test_backend`, `test_server` |
| Sweeper & turns | `test_sweeper`, `test_turns` |
| Tracing | `test_tracing` |
| Chat UI | `test_app_html` |

A few carry their weight beyond the happy path — e.g. `test_failure_detection`
exercises the atomic diagnosis claim and the transient-retry behavior, and
`test_async_chat` checks that the incident is filed for the run's *actual* host and
never double-filed.

## The real-model eval (`eval/routing_eval.py`)

This is the **one** thing that calls the real model. It checks a single question:
*does the agent route each prompt to the right tool?* The tools are mocked
(record-only, so nothing real is triggered), but the **model does the routing**.

```bash
DATABRICKS_CONFIG_PROFILE=<profile> PYTHONPATH=. uv run python eval/routing_eval.py
```

It's **not** part of the normal loop — it needs Databricks auth and spends real
model calls — so run it deliberately, not on every change.

### Reading its result correctly

The eval runs each prompt **in isolation** — a fresh `route_and_run` with no
conversation memory. That matters for interpreting the score:

- `investigate <host>` prompts and out-of-scope prompts ("hello", "tell me a joke")
  route correctly, because they need no prior context.
- `check_status` / `diagnose_run` prompts route to **no tool** in isolation — those
  tools need a `run_id`, and with no prior turn there's none in context, so the model
  correctly declines rather than inventing one (its system prompt forbids inventing
  ids). In the real app that `run_id` comes from memory (a prior `investigate` turn),
  which the eval doesn't set up.

So a middling accuracy number is expected by construction; the eval's real value is
**comparing configurations and catching routing regressions** (e.g. confirming a
change to the agent builder routes identically), and validating the investigate/
decline behavior. Interpret it as a diff tool, not an absolute grade.

## The one live check the tests can't replace

Because the suite mocks the model, Lakebase, and Jobs, it proves *structure and
logic* but not that the deployed app actually boots and round-trips. That's why
changes to the agent core or dependencies are also **deploy-verified** against the
live app. The offline suite is the fast gate; a real `/chat` round-trip is the final
one.

---

That's the end of the walkthrough. Head back to the [index & overview](README.md)
for the map, or jump to any section from there.
