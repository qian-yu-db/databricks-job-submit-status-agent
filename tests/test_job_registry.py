import time
import agent.job_registry as jr


def _spec(**kw):
    base = dict(job_key="investigation", display_name="Investigation",
                description="Investigate a host", databricks_job_id=363941843397741,
                param_schema={"host": {"type": "string", "required": True}},
                fixed_params={}, requires_approval=False, allowed_groups=[], enabled=True)
    base.update(kw)
    return jr.JobSpec(**base)


def test_resolve_job_happy_path_returns_id_and_params():
    reg = {"investigation": _spec()}
    job_id, params = jr.resolve_job("investigation", {"host": "web-prod-04"}, reg)
    assert job_id == 363941843397741
    assert params["host"] == "web-prod-04"


def test_resolve_job_fixed_params_override_caller():
    reg = {"failure_demo": _spec(job_key="failure_demo", param_schema={},
                                 fixed_params={"host": "fail-demo-01"})}
    job_id, params = jr.resolve_job("failure_demo", {"host": "web-prod-04"}, reg)
    assert params["host"] == "fail-demo-01"   # server-forced wins


def test_resolve_job_unknown_key_raises():
    import pytest
    with pytest.raises(jr.RegistryError):
        jr.resolve_job("nope", {}, {"investigation": _spec()})


def test_resolve_job_disabled_key_raises():
    import pytest
    reg = {"investigation": _spec(enabled=False)}
    with pytest.raises(jr.RegistryError):
        jr.resolve_job("investigation", {"host": "h"}, reg)


def test_resolve_job_missing_required_param_raises():
    import pytest
    with pytest.raises(jr.RegistryError):
        jr.resolve_job("investigation", {}, {"investigation": _spec()})


def test_resolve_job_accepts_falsy_but_present_required_value():
    """A required param whose legitimate value is falsy (0, False) is accepted —
    only None/'' count as missing."""
    reg = {"k": _spec(job_key="k", param_schema={"count": {"required": True}})}
    job_id, params = jr.resolve_job("k", {"count": 0}, reg)
    assert params["count"] == 0


def test_load_jobs_parses_enabled_rows(monkeypatch):
    rows = [
        {"job_key": "investigation", "display_name": "Investigation", "description": "d",
         "databricks_job_id": 363941843397741,
         "param_schema": {"host": {"type": "string", "required": True}},
         "fixed_params": {}, "requires_approval": False, "allowed_groups": [], "enabled": True},
    ]
    class _Cur:
        def execute(self, *a): pass
        def fetchall(self): return rows
        def __enter__(self): return self
        def __exit__(self, *a): return False
    class _Conn:
        def cursor(self): return _Cur()
    reg = jr.load_jobs(_Conn())
    assert "investigation" in reg and reg["investigation"].databricks_job_id == 363941843397741


def test_get_registry_caches_within_ttl(monkeypatch):
    calls = {"n": 0}
    def fake_load(conn):
        calls["n"] += 1
        return {"investigation": _spec()}
    monkeypatch.setattr(jr, "load_jobs", fake_load)
    monkeypatch.setattr(jr, "_open_conn", lambda: _DummyConn())
    jr._CACHE["data"] = None; jr._CACHE["ts"] = 0.0
    jr.get_registry(); jr.get_registry()
    assert calls["n"] == 1          # second call served from cache


def test_get_registry_unavailable_with_no_cache_raises(monkeypatch):
    import pytest
    monkeypatch.setattr(jr, "_open_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    jr._CACHE["data"] = None; jr._CACHE["ts"] = 0.0
    with pytest.raises(jr.RegistryUnavailable):
        jr.get_registry(force=True)


def test_get_registry_serves_last_good_cache_on_transient_failure(monkeypatch):
    warm = {"investigation": _spec()}
    monkeypatch.setattr(jr, "_open_conn", lambda: (_ for _ in ()).throw(RuntimeError("db blip")))
    jr._CACHE["data"] = warm; jr._CACHE["ts"] = 0.0   # expired TTL → refresh attempted
    result = jr.get_registry(force=True)               # no exception
    assert result is warm                              # last-good copy returned


class _DummyConn:
    def close(self): pass
    def cursor(self): raise AssertionError("load_jobs is faked in this test")
