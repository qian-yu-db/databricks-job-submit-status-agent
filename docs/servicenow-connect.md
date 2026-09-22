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

## Do you need to register the MCP with Unity AI Gateway?

Registering the MCP app as a **Unity Catalog MCP Service** is **not required for the
agent to work** — the orchestrator calls the MCP app directly today, and the
**human approval gate is enforced in the app**, not the gateway. What registration
adds is **governance + managed per-user OBO**: gateway usage tracking, payload
logging, Service Policies (allow/deny/**ask** on the write), tool discovery in Unity
Catalog, and — the important one — **Databricks-managed per-user OAuth to ServiceNow**
so the ticket is created *as the actual user*.

So the only thing you lose functionally by **not** registering is the *turnkey*
per-user identity to ServiceNow: without it, ServiceNow sees a single shared identity
(attribution via `caller_id`) unless you implement per-user OAuth in the MCP yourself
(Path B2 below).

> **First principle:** Databricks on-behalf-of gives your app a **Databricks** user
> token — that is **not** a ServiceNow token. Something in the chain must exchange it
> for a **per-user ServiceNow** OAuth token. *Where* you do that is the choice below.

## Paths

| Path | Incident filed as | You own | Use when |
|------|-------------------|---------|----------|
| **A. Single service token** | one service account | just config | a fast "it really hit ServiceNow" demo |
| **B1. Per-user OBO — managed** (UC Connection + MCP Service) | the actual user | grants + connection; Databricks owns the OAuth | you want managed tokens + gateway governance |
| **B2. Per-user OBO — custom** (the MCP does its own OAuth) | the actual user | the ServiceNow OAuth code | you can't/don't want the managed connection path |

> **What's implemented today:** the MCP's REST call uses a bearer token from
> `_obo_token()`, which returns the static `SERVICENOW_OBO_TOKEN` — i.e. **Path A**.
> Both B1 and B2 are the *recommended architecture to finish*, not a config-only flip.

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

## Path B — per-user OBO (two ways)

Both make the ticket land **as the actual user**; they differ in *who owns the
ServiceNow OAuth*.

- **B1 — managed:** register the MCP as a **UC MCP Service** backed by a **UC HTTP
  Connection** using per-user OAuth (U2M). Databricks runs the OAuth flow + refresh
  and injects the caller's token — the MCP never handles long-lived secrets — and you
  also get **Service Policies** (allow/deny/**ask** on the write) and gateway audit.
  > **Check the OAuth `resource` requirement first:** some OAuth providers require the
  > RFC 8707 `resource` parameter during authorization/token exchange. Confirm whether
  > your ServiceNow OAuth needs it **and** that your UC HTTP Connection can send it
  > before committing to B1.
- **B2 — custom:** the MCP implements ServiceNow OAuth U2M itself (no UC Connection).
  You own token storage/refresh, which avoids any connection-layer OAuth limitations —
  at the cost of more code. **Key each token to the authenticated Databricks identity
  (from the forwarded token / `x-forwarded-email`), never to a tool argument like
  `caller_id`.**

> **The code step, either way:** `_obo_token()` in `servicenow_mcp/server.py` returns
> the static `SERVICENOW_OBO_TOKEN` today. For **B1** it must return the caller's token
> the MCP Service / connection provides per request; for **B2** it (or the backend) must
> run/return the per-user ServiceNow OAuth token. Until wired, the REST backend
> **fails fast** rather than sending an empty bearer.

### B1 — managed (UC Connection + MCP Service)

#### 1. Register an OAuth app in ServiceNow *(ServiceNow admin)*
In **System OAuth → Application Registry**, create an OAuth API endpoint for an
external client. Record the **client id/secret**, **authorization** and **token**
endpoints, and set the redirect URL Databricks gives you. Grant it the scope needed
to create incidents.

#### 2. Create a Unity Catalog HTTP Connection (`OAUTH_U2M`) *(Databricks)*
Create a UC **HTTP Connection** to ServiceNow using **per-user OAuth (U2M)**, with
the client id/secret and endpoints from step 1. Databricks stores the grant and,
at call time, retrieves and injects the per-user credential — secrets stay in UC.
See [Connect to external HTTP services](https://docs.databricks.com/aws/en/query-federation/http).

#### 3. Register this MCP as an AI Gateway MCP Service *(Databricks)*
Register the deployed ServiceNow MCP app as a governed **MCP Service** backed by the
connection from step 2, with authentication **OAuth U2M Per User**. The Gateway then
routes the caller's identity to the connection and injects the downscoped ServiceNow
token into the request.
- [Connect agents to tools with MCP Services](https://docs.databricks.com/aws/en/agents/mcp-tools/mcp-services)
- [Register an external MCP server](https://docs.databricks.com/aws/en/ai-gateway/register-mcp-service)

#### 4. Point the MCP at your instance and wire the per-user token *(this repo)*
Set `SERVICENOW_BACKEND=rest` and `SERVICENOW_INSTANCE=https://<your-instance>.service-now.com`
in **`resources/apps.yml`** (the `servicenow_mcp` app's `config.env`, which the bundle
deploys — not `servicenow_mcp/app.yaml`), and redeploy. Then complete the code
extension point: make `_obo_token()` (in `servicenow_mcp/server.py`) return the
**caller's** per-user ServiceNow token that the MCP Service / connection provides for
the current request, instead of the static `SERVICENOW_OBO_TOKEN`. Until that wiring
is in place the REST backend has no per-user token and **fails fast** with a clear
error (it does not silently send an empty bearer).

#### 5. Grants and scopes *(Databricks)*
- Grant the users (and the orchestrator app's service principal) `CAN_USE` on the
  MCP app and `EXECUTE`/access on the MCP Service — deny-by-default: a user who
  isn't granted it can't reach ServiceNow.
- The orchestrator already declares `user_api_scopes: [ai-gateway]` so the user
  token forwards to the Gateway ([Configure authorization in a Databricks app](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/auth)).

#### 6. Consent and verify
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

### B2 — custom (the MCP owns the ServiceNow OAuth)

Use this when you can't use the managed connection path (e.g. the `resource`-parameter
requirement above). The MCP performs the ServiceNow OAuth itself:

1. Enable user authorization on the orchestrator (`user_api_scopes: [ai-gateway]`,
   `get_user_workspace_client()`) so the MCP receives **authenticated Databricks context**.
2. In the MCP server, run **ServiceNow OAuth U2M per user** (consent + token exchange).
3. Store the refresh/access token **server-side, keyed to the authenticated Databricks
   user** — never to a tool argument.
4. `_obo_token()` returns that user's ServiceNow token; set `SERVICENOW_BACKEND=rest`
   + `SERVICENOW_INSTANCE` in `resources/apps.yml`, and redeploy.

You own the OAuth implementation and its security, but you avoid the UC-Connection
OAuth layer entirely — so provider `resource`-parameter limitations don't apply.

---

## Notes

- Nothing here changes the agent or the approval gate — only the MCP's backend and
  how its token is sourced. The stub and the real paths present the identical
  `create_incident` contract.
- The MCP server code lives in `servicenow_mcp/` (`server.py`, `backend.py`); the
  backend switch is `SERVICENOW_BACKEND` (`stub` | `rest`).
- ServiceNow is **not** a Databricks *managed*-OAuth provider today, so the custom
  MCP + UC Connection is the current path; a managed ServiceNow connector is on the
  roadmap.
