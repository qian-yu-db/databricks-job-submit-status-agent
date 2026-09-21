import sys
import types


def _fake_mlflow():
    """A stand-in mlflow module recording the calls enable_tracing makes."""
    calls = {"tracking_uri": None, "experiment": None, "autolog": 0}
    m = types.ModuleType("mlflow")
    m.set_tracking_uri = lambda uri: calls.__setitem__("tracking_uri", uri)
    m.set_experiment = lambda exp: calls.__setitem__("experiment", exp)
    lc = types.ModuleType("mlflow.langchain")
    lc.autolog = lambda: calls.__setitem__("autolog", calls["autolog"] + 1)
    m.langchain = lc
    return m, calls


def _install(monkeypatch, m):
    monkeypatch.setitem(sys.modules, "mlflow", m)
    monkeypatch.setitem(sys.modules, "mlflow.langchain", m.langchain)


def test_enable_tracing_sets_experiment_when_configured(monkeypatch):
    import agent.tracing as t
    m, calls = _fake_mlflow()
    _install(monkeypatch, m)
    monkeypatch.setenv("MLFLOW_EXPERIMENT", "/Users/you@example.com/job-status-agent")
    t.enable_tracing()
    assert calls["tracking_uri"] == "databricks"
    assert calls["experiment"] == "/Users/you@example.com/job-status-agent"
    assert calls["autolog"] == 1


def test_enable_tracing_local_default_no_experiment(monkeypatch):
    """No MLFLOW_EXPERIMENT (local dev): don't force the databricks URI; just autolog."""
    import agent.tracing as t
    m, calls = _fake_mlflow()
    _install(monkeypatch, m)
    monkeypatch.delenv("MLFLOW_EXPERIMENT", raising=False)
    t.enable_tracing()
    assert calls["tracking_uri"] is None
    assert calls["experiment"] is None
    assert calls["autolog"] == 1


def test_enable_tracing_is_non_fatal(monkeypatch):
    import agent.tracing as t
    m, calls = _fake_mlflow()
    m.langchain.autolog = lambda: (_ for _ in ()).throw(RuntimeError("mlflow down"))
    _install(monkeypatch, m)
    t.enable_tracing()  # must NOT raise
