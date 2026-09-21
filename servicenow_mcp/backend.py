from typing import Protocol, Callable, Any
import httpx

class ServiceNowBackend(Protocol):
    def create_incident(self, *, short_description: str, description: str,
                         caller_id: str) -> dict[str, Any]: ...

class StubBackend:
    """In-memory ServiceNow stand-in; echoes the authenticated caller."""
    def __init__(self) -> None:
        self._seq = 42000

    def create_incident(self, *, short_description: str, description: str,
                        caller_id: str) -> dict[str, Any]:
        self._seq += 1
        return {"number": f"INC{self._seq:07d}", "sys_id": f"stub-{self._seq}",
                "caller_id": caller_id, "short_description": short_description,
                "state": "New"}

class RestBackend:
    """Real ServiceNow Table API. The bearer token comes from `token_provider`
    (see servicenow_mcp/server.py); wiring for per-user OBO is in
    docs/servicenow-connect.md.

    `caller_id` is the originating user's email, passed through for attribution.
    ServiceNow's incident `caller_id` is a reference field (expects a sys_id), so on
    a real instance you may need a sys_user lookup to resolve the email to a user
    reference — otherwise the caller may land unset. The stub echoes it verbatim."""
    def __init__(self, instance: str, token_provider: Callable[[], str],
                 client: httpx.Client | None = None) -> None:
        self._instance = instance.rstrip("/")
        self._token_provider = token_provider
        # Benign for the long-lived MCP singleton. If per-user OBO ever constructs a
        # RestBackend per request, inject a shared client (or close this one) so the
        # connection pool isn't leaked per call.
        self._client = client or httpx.Client(timeout=30.0)

    def create_incident(self, *, short_description: str, description: str,
                        caller_id: str) -> dict[str, Any]:
        token = self._token_provider()
        if not token:
            raise RuntimeError(
                "ServiceNow token not configured: set SERVICENOW_OBO_TOKEN (single-token "
                "path) or complete the per-user OBO wiring — see docs/servicenow-connect.md")
        resp = self._client.post(
            f"{self._instance}/api/now/table/incident",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"short_description": short_description, "description": description,
                  "caller_id": caller_id},
        )
        resp.raise_for_status()
        return resp.json()["result"]
