import os
from mcp.server.fastmcp import FastMCP

# Dual-mode import: works both as a package (`servicenow_mcp.server`, used by the
# orchestrator + tests) and when this file is the deployed app's entrypoint run
# from inside its own source dir (`python server.py`), where only these files ship.
try:
    from servicenow_mcp.backend import ServiceNowBackend, StubBackend, RestBackend
except ModuleNotFoundError:
    from backend import ServiceNowBackend, StubBackend, RestBackend

def _obo_token() -> str:
    # Bearer token for RestBackend. Today this returns the static SERVICENOW_OBO_TOKEN
    # env — the single-token path, where every incident is filed as that one account.
    #
    # EXTENSION POINT for per-user OBO: to file as the *actual* user, this must return
    # the CALLER's downscoped per-user ServiceNow token, obtained via the UC Connection
    # (OAUTH_U2M Per User) behind an AI Gateway MCP Service. That per-request token
    # wiring is NOT implemented here — see docs/servicenow-connect.md ("Path B").
    return os.environ.get("SERVICENOW_OBO_TOKEN", "")

def build_backend(env: dict) -> ServiceNowBackend:
    if env.get("SERVICENOW_BACKEND", "stub") == "rest":
        instance = env.get("SERVICENOW_INSTANCE")
        if not instance:
            raise ValueError(
                "SERVICENOW_INSTANCE is required when SERVICENOW_BACKEND=rest")
        return RestBackend(instance=instance, token_provider=_obo_token)
    return StubBackend()

def create_incident_tool(backend: ServiceNowBackend, *, short_description: str,
                         description: str, caller_id: str) -> dict:
    return backend.create_incident(short_description=short_description,
                                   description=description, caller_id=caller_id)

mcp = FastMCP("servicenow-mcp")
_backend = build_backend(os.environ)

@mcp.tool()
def create_incident(short_description: str, description: str, caller_id: str) -> dict:
    """Create a ServiceNow incident as the given caller. Returns the incident record."""
    return create_incident_tool(_backend, short_description=short_description,
                                description=description, caller_id=caller_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
