import json
import httpx
import pytest
from servicenow_mcp.backend import StubBackend, RestBackend

def test_stub_creates_incident_as_caller():
    b = StubBackend()
    inc = b.create_incident(short_description="sd", description="d", caller_id="ryan@bwater.com")
    assert inc["number"].startswith("INC")
    assert inc["caller_id"] == "ryan@bwater.com"
    assert inc["state"] == "New"

def test_stub_increments_number():
    b = StubBackend()
    a = b.create_incident(short_description="x", description="x", caller_id="u")
    c = b.create_incident(short_description="y", description="y", caller_id="u")
    assert a["number"] != c["number"]

def test_rest_posts_bearer_and_caller():
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"result": {"number": "INC0042001",
            "sys_id": "abc", "caller_id": "ryan@bwater.com", "state": "1"}})
    transport = httpx.MockTransport(handler)
    b = RestBackend(instance="https://dev1.service-now.com",
                    token_provider=lambda: "tok-123",
                    client=httpx.Client(transport=transport))
    inc = b.create_incident(short_description="sd", description="d", caller_id="ryan@bwater.com")
    assert inc["number"] == "INC0042001"
    assert captured["auth"] == "Bearer tok-123"
    assert captured["url"].endswith("/api/now/table/incident")
    assert captured["body"]["caller_id"] == "ryan@bwater.com"
    assert captured["body"]["short_description"] == "sd"


def test_rest_fails_fast_on_empty_token():
    # Empty token (e.g. rest mode without SERVICENOW_OBO_TOKEN and no per-user wiring)
    # must raise a clear error, not send an empty bearer.
    b = RestBackend(instance="https://dev1.service-now.com", token_provider=lambda: "")
    with pytest.raises(RuntimeError, match="token not configured"):
        b.create_incident(short_description="sd", description="d", caller_id="u@x.com")


def test_build_backend_requires_instance_for_rest():
    from servicenow_mcp.server import build_backend
    with pytest.raises(ValueError, match="SERVICENOW_INSTANCE is required"):
        build_backend({"SERVICENOW_BACKEND": "rest"})       # no SERVICENOW_INSTANCE
    # stub mode never requires it
    assert build_backend({}).__class__.__name__ == "StubBackend"
