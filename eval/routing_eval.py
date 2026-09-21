# eval/routing_eval.py  — run: uv run python eval/routing_eval.py  (needs DATABRICKS auth)
"""Routing eval: does the agent pick the right tool for each prompt?
Tools are mocked (record-only) so nothing real is triggered; the REAL model routes."""
import agent.llm_agent as la
from agent.status_store import InMemoryStatusStore

CASES = [
    ("investigate web-prod-04", "investigate"),
    ("look into the prod web box", "investigate"),
    ("check db-replica-07", "investigate"),
    ("what's the status?", "check_status"),
    ("how's my run going?", "check_status"),
    ("is it done yet?", "check_status"),
    ("why did it fail?", "diagnose_run"),
    ("diagnose the failed run", "diagnose_run"),
    ("what went wrong with the job?", "diagnose_run"),
    ("hello", None),
    ("what can you do?", None),
    ("tell me a joke", None),
]

def run():
    import mlflow
    fired = []
    # monkeypatch tool impls to record instead of execute
    la._investigate_impl = lambda *a, **k: (fired.append("investigate"), "ok")[1]
    la._check_status_impl = lambda *a, **k: (fired.append("check_status"), "ok")[1]
    la._diagnose_impl = lambda *a, **k: (fired.append("diagnose_run"), "ok")[1]
    passed = 0
    with mlflow.start_run(run_name="routing-eval"):
        for prompt, expected in CASES:
            fired.clear()
            try:
                la.route_and_run(prompt, "r1", "web-prod-04", "u@x.com", "tok", InMemoryStatusStore())
            except Exception:
                pass
            got = fired[0] if fired else None
            ok = got == expected
            passed += ok
            print(f"{'PASS' if ok else 'FAIL'}: {prompt!r} -> {got} (want {expected})")
        acc = passed / len(CASES)
        mlflow.log_metric("routing_accuracy", acc)
        print(f"\nrouting_accuracy = {acc:.2f} ({passed}/{len(CASES)})")

if __name__ == "__main__":
    run()
