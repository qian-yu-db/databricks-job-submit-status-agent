"""OBO SQL identity passthrough: the auth-log query runs under the user's token.

Guards the `auth_type="pat"` fix — without it the Databricks SDK errors with
"more than one authorization method configured: oauth and pat", because the
Apps runtime injects the service principal's DATABRICKS_CLIENT_ID/SECRET which
collide with the forwarded user token.

`Config` is patched with a recorder so no real credential/OIDC resolution runs.
"""

import agent.app as app_mod


def test_obo_investigate_builds_pure_token_config(monkeypatch):
    captured = {}

    class _FakeConfig:
        def __init__(self, host=None, token=None, auth_type=None):
            self.host = host
            self.token = token
            self.auth_type = auth_type
            captured["cfg"] = self

    class _FakeWC:
        def __init__(self, config=None):
            captured["wc_config"] = config

    def _fake_executor(wc, warehouse_id):
        captured["warehouse_id"] = warehouse_id
        return lambda sql, params: []

    def _fake_investigate(host, *, execute_sql, table):
        captured["host"] = host
        captured["table"] = table
        return type("F", (), {"summary": "s", "severity": "high"})()

    # Config, investigate and sdk_sql_executor are imported inside
    # obo_investigate; patch them at their source modules.
    import databricks.sdk.core as sdk_core
    import agent.investigation as inv
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setattr(sdk_core, "Config", _FakeConfig)
    monkeypatch.setattr(app_mod, "WorkspaceClient", _FakeWC)
    monkeypatch.setattr(inv, "sdk_sql_executor", _fake_executor)
    monkeypatch.setattr(inv, "investigate", _fake_investigate)
    monkeypatch.setattr(app_mod, "WAREHOUSE_ID", "wh-123")
    monkeypatch.setattr(app_mod, "AUTH_TABLE", "cat.sch.auth_events")

    app_mod.obo_investigate("web-prod-04", "user-token-abc")

    cfg = captured["cfg"]
    assert cfg.auth_type == "pat"          # the fix: force pure token auth
    assert cfg.token == "user-token-abc"   # the USER's forwarded token, not the SP
    assert cfg.host == "https://example.cloud.databricks.com"  # from DATABRICKS_HOST env
    assert captured["wc_config"] is cfg    # WorkspaceClient uses that user config
    assert captured["warehouse_id"] == "wh-123"
    assert captured["table"] == "cat.sch.auth_events"
