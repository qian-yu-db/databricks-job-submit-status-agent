def user_identity(headers: dict) -> str:
    """Resolve the originating user from Databricks Apps forwarded headers."""
    return headers.get("x-forwarded-email") or headers.get("x-forwarded-user") or "unknown"

def get_user_client():
    """Per-user workspace client (OBO). Called INSIDE the request handler only."""
    from databricks_app.utils import get_user_workspace_client
    return get_user_workspace_client()
