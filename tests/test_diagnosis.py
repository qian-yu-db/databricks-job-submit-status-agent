import agent.diagnosis as dg


class _FakeModel:
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)

        class R:
            content = "Root cause: missing table auth_events_stg. Fix: create it / grant SELECT."

        return R()


def test_diagnose_is_grounded_and_toolless():
    m = _FakeModel()
    out = dg.diagnose("AnalysisException ... auth_events_stg", model=m)
    assert "auth_events_stg" in out
    # exactly one system + one human message; the untrusted-data framing is present
    msgs = m.calls[0]
    assert len(msgs) == 2
    assert "untrusted" in msgs[0].content.lower()


def test_diagnose_flattens_reasoning_model_blocks():
    """claude-sonnet-5 returns .content as a list of blocks; diagnose() must
    return plain text, never the raw block list (which leaked into [diagnosis])."""
    class _BlockModel:
        def invoke(self, messages):
            class R:
                content = [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "", "signature": "s"}]},
                    {"type": "text", "text": "Root cause: missing auth_events_stg. Fix: create it."},
                ]
            return R()

    out = dg.diagnose("AnalysisException ... auth_events_stg", model=_BlockModel())
    assert out == "Root cause: missing auth_events_stg. Fix: create it."
    assert "reasoning" not in out and "signature" not in out


def test_fetch_run_error_reads_task_output():
    class _Out:
        error = "boom"
        error_trace = "trace"
        logs = "...log tail..."

    class _Task:
        run_id = 42

    class _Run:
        tasks = [_Task()]

    class _Jobs:
        def get_run(self, run_id):
            return _Run()

        def get_run_output(self, run_id):
            assert run_id == 42
            return _Out()

    class _WC:
        jobs = _Jobs()

    text = dg.fetch_run_error(_WC(), 999)
    assert "boom" in text and "trace" in text
