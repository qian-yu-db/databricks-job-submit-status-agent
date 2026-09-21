from servicenow_mcp.backend import StubBackend
from servicenow_mcp.server import build_backend, create_incident_tool

def test_build_backend_stub_default():
    assert type(build_backend({"SERVICENOW_BACKEND": "stub"})).__name__ == "StubBackend"

def test_create_incident_tool_returns_number():
    out = create_incident_tool(StubBackend(), short_description="Suspicious logins",
                               description="host web-prod-04", caller_id="ryan@bwater.com")
    assert out["number"].startswith("INC")
    assert out["caller_id"] == "ryan@bwater.com"
