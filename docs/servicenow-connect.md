# Connecting a real ServiceNow instance

By default the ServiceNow MCP app runs a **stub** backend: it accepts the tool
call, echoes the authenticated caller, and returns a fake incident number. That's
enough to prove the whole flow — investigate → status → **human approval** →
"incident filed as the user" — without any ServiceNow at all.

This guide is for the step you do once you *have* ServiceNow: pointing the MCP at a
real instance so incidents are created there, ideally **as the calling user**.

## The two hops

Identity reaches ServiceNow in two hops. The stub proves hop A today; this guide
completes hop B.

```mermaid
flowchart LR
    user([User]) -->|"hop A: Databricks identity"| mcp["ServiceNow MCP app<br/>(this repo)"]
    mcp -->|"hop B: ServiceNow OAuth token"| snow[("Your ServiceNow<br/>instance")]
```

- **Hop A** (user → MCP) is Databricks-native and already works: the orchestrator
  passes the originating user through, and the incident is attributed to them
  (`caller_id`). See [Identity & passthrough](04-identity-and-passthrough.md).
- **Hop B** (MCP → ServiceNow) is what you wire up here. The recommended form is
  **per-user OBO**: each user's Databricks identity is exchanged for a short-lived
  ServiceNow token, so the incident is created *as that user* — the auditing and
  access-control story ServiceNow customers want.

## Two paths

| Path | Incident filed as | Status in this repo | Use when |
|------|-------------------|---------------------|----------|
| **A. Single service token** | one service account | **works today** (config-only) | a fast "it really hit ServiceNow" demo |
| **B. Per-user OBO** (recommended) | the actual signed-in user | **target — needs token wiring** | the real security story |

> **What's implemented today:** the MCP's REST call to ServiceNow uses a bearer
> token from `_obo_token()`, which currently returns the static `SERVICENOW_OBO_TOKEN`
> — i.e. **Path A**. **Path B is the recommended architecture to finish, not a
> config-only flip:** it needs `_obo_token()` wired to return the *caller's*
> per-user token (see the gap note in Path B). Steps 1–3 set up the Databricks side;
> step 4 is the code step.

---

## Path A — single service token (quickest)

1. In ServiceNow, get an API token (or basic-auth-derived bearer) for a service
   account that can create incidents.
2. Set the MCP app's environment. For the **bundle-deployed** app, edit the
   `servicenow_mcp` app's `config.env` in **`resources/apps.yml`** — that is the env
   the deploy applies (the source `servicenow_mcp/app.yaml` is only for standalone/
   local runs). The commented block there shows exactly where:
   - `SERVICENOW_BACKEND=rest`
   - `SERVICENOW_INSTANCE=https://<your-instance>.service-now.com`
   - `SERVICENOW_OBO_TOKEN=<the service-account token>`
3. Redeploy the MCP app: `databricks bundle deploy` then `databricks bundle run servicenow_mcp`.

Every incident is now created in your instance — but all as the one service
account. Honest framing: this proves the REST integration, **not** per-user OBO.

---

## Path B — per-user OBO (recommended)

This uses Databricks' **managed** OBO to an external service. Databricks holds the
OAuth grant and provides each caller's downscoped ServiceNow token — the MCP never
handles long-lived secrets. This is the differentiator worth showing.

> **Gap to close first (code):** the MCP calls ServiceNow with the token from
> `_obo_token()`, which today only reads the static env var. For per-user OBO,
> `_obo_token()` must return the **caller's** downscoped token that the MCP
> Service / UC Connection provides *for the current request*. That per-request
> wiring — per the [MCP Services docs](https://docs.databricks.com/aws/en/agents/mcp-tools/mcp-services) —
> is the code step this path depends on (step 4). Steps 1–3 are the Databricks setup.

### 1. Register an OAuth app in ServiceNow *(ServiceNow admin)*
In **System OAuth → Application Registry**, create an OAuth API endpoint for an
external client. Record the **client id/secret**, **authorization** and **token**
endpoints, and set the redirect URL Databricks gives you. Grant it the scope needed
to create incidents.

### 2. Create a Unity Catalog HTTP Connection (`OAUTH_U2M`) *(Databricks)*
Create a UC **HTTP Connection** to ServiceNow using **per-user OAuth (U2M)**, with
the client id/secret and endpoints from step 1. Databricks stores the grant and,
at call time, retrieves and injects the per-user credential — secrets stay in UC.
See [Connect to external HTTP services](https://docs.databricks.com/aws/en/query-federation/http).

### 3. Register this MCP as an AI Gateway MCP Service *(Databricks)*
Register the deployed ServiceNow MCP app as a governed **MCP Service** backed by the
connection from step 2, with authentication **OAuth U2M Per User**. The Gateway then
routes the caller's identity to the connection and injects the downscoped ServiceNow
token into the request.
- [Connect agents to tools with MCP Services](https://docs.databricks.com/aws/en/agents/mcp-tools/mcp-services)
- [Register an external MCP server](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service)

### 4. Point the MCP at your instance and wire the per-user token *(this repo)*
Set `SERVICENOW_BACKEND=rest` and `SERVICENOW_INSTANCE=https://<your-instance>.service-now.com`
in **`resources/apps.yml`** (the `servicenow_mcp` app's `config.env`, which the bundle
deploys — not `servicenow_mcp/app.yaml`), and redeploy. Then complete the code
extension point: make `_obo_token()` (in `servicenow_mcp/server.py`) return the
**caller's** per-user ServiceNow token that the MCP Service / connection provides for
the current request, instead of the static `SERVICENOW_OBO_TOKEN`. Until that wiring
is in place the REST backend has no per-user token and **fails fast** with a clear
error (it does not silently send an empty bearer).

### 5. Grants and scopes *(Databricks)*
- Grant the users (and the orchestrator app's service principal) `CAN_USE` on the
  MCP app and `EXECUTE`/access on the MCP Service — deny-by-default: a user who
  isn't granted it can't reach ServiceNow.
- The orchestrator already declares `user_api_scopes: [ai-gateway]` so the user
  token forwards to the Gateway ([Configure authorization in a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)).

### 6. Consent and verify
On a user's first incident, Databricks prompts a one-time ServiceNow OAuth consent;
tokens are minted and refreshed thereafter. Then run the demo end to end:

1. "Investigate web-prod-04" → job runs, status streams.
2. On completion the agent **proposes** the incident and waits.
3. Click **Approve & file**.
4. Open the incident in ServiceNow — it was created **with the actual user's
   token** (`opened_by` reflects that user). Note: `caller_id` is a reference field
   (sys_id); if you passed an email you may need a `sys_user` lookup to resolve it,
   or the `caller` can land unset even though the request authenticated as the user.

Creating the incident with the user's own token is the proof of criterion 3
(end-to-end user identity to a downstream system) against a real external service.

---

## Notes

- Nothing here changes the agent or the approval gate — only the MCP's backend and
  how its token is sourced. The stub and both real paths present the identical
  `create_incident` contract.
- The MCP server code lives in `servicenow_mcp/` (`server.py`, `backend.py`); the
  backend switch is `SERVICENOW_BACKEND` (`stub` | `rest`).
- ServiceNow is **not** a Databricks *managed*-OAuth provider today, so the custom
  MCP + UC Connection is the current path; a managed ServiceNow connector is on the
  roadmap.
