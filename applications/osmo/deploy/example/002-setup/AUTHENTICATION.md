# OSMO Authentication (Keycloak and Nebius SSO)

This document describes how authentication works for OSMO on Nebius and how to integrate **Nebius System SSO** as the primary identity provider, aligning with Nebius internal authentication standards.

## Overview

- **Keycloak** provides authentication for the OSMO control plane: Web UI, REST API, and CLI.
- By default, a local test user (`osmo-admin` / `osmo-admin`) is created for development.
- For production, **Nebius SSO** can be configured as the primary IdP so users log in with corporate credentials; default username/password is then not created.
- **Google, GitHub, and Microsoft (Azure)** can be added as optional social IdPs so users see "Google", "GitHub", and "Microsoft" on the Keycloak login page (see [Google / GitHub / Microsoft SSO](#google--github--microsoft-sso) below).

## Authentication Flow

### Browser / Web UI

1. User opens OSMO Web UI (e.g. `https://<OSMO_INGRESS_HOSTNAME>`).
2. Envoy sidecar on OSMO services redirects unauthenticated requests to Keycloak.
3. **If Nebius SSO is enabled:** Keycloak redirects to Nebius SSO (OIDC); user signs in with corporate credentials; Nebius SSO redirects back to Keycloak with an authorization code; Keycloak issues a session and redirects to OSMO with cookies.
4. **If only local users:** User signs in on Keycloak’s login form (e.g. `osmo-admin` / `osmo-admin`).
5. Keycloak issues JWTs; Envoy validates them and allows access to OSMO API/UI.

### REST API / CLI

- **Web UI (redirect flow):** Opening the OSMO URL in a browser uses the **authorization code flow**: redirect to Keycloak → Nebius SSO (or local login) → redirect back to `https://<OSMO_HOST>/oauth2/callback` with no manual code. That is the expected SSO experience.
- **CLI `osmo login`:** The OSMO server may return the **device authorization flow** for the CLI. The CLI then opens a browser to Keycloak’s device page and should print a **user code in the terminal** for you to enter there. If the terminal never shows a code, use the dev (password) method below instead.
- **CLI without device code (recommended with SSO):** Use the **dev** method with the test user so the CLI logs in via password and never opens the device page:  
  `osmo login https://<OSMO_HOST> --method dev --username osmo-admin`  
  (Requires the test user to exist: set `CREATE_OSMO_TEST_USER=true` before 04, or create a user in Keycloak.)
- **Resource owner password grant (automation):** With local users, scripts can obtain a JWT using `client_id=osmo-device` and username/password. When Nebius SSO is primary, there is no default local user; use a break-glass user (see below) or the dev method above.

### Backend operator (05-deploy-osmo-backend.sh)

- The backend deploy script creates a service token by calling the OSMO token API with a JWT.
- It obtains that JWT via **password grant** using `OSMO_KC_ADMIN_USER` / `OSMO_KC_ADMIN_PASS` (default `osmo-admin` / `osmo-admin`).
- **When Nebius SSO is primary:** The default local user is not created. Either:
  - Set **`CREATE_OSMO_TEST_USER=true`** before running `04-deploy-osmo-control-plane.sh` so the `osmo-admin` user exists for the backend script, or
  - Create a dedicated local user in Keycloak for automation and set `OSMO_KC_ADMIN_USER` / `OSMO_KC_ADMIN_PASS` when running `05-deploy-osmo-backend.sh`.

## Google / GitHub / Microsoft SSO

You can enable **Gmail (Google), GitHub, and Azure (Microsoft)** login alongside or instead of Nebius SSO. Set the following in `osmo-deploy.env` (or export before running `04-deploy-osmo-control-plane.sh`):

| IdP       | Env vars | Redirect URI to register in the provider |
|-----------|----------|------------------------------------------|
| **Google**  | `GOOGLE_SSO_CLIENT_ID`, `GOOGLE_SSO_CLIENT_SECRET` | `https://<AUTH_DOMAIN>/realms/osmo/broker/google/endpoint` |
| **GitHub**  | `GITHUB_SSO_CLIENT_ID`, `GITHUB_SSO_CLIENT_SECRET` | `https://<AUTH_DOMAIN>/realms/osmo/broker/github/endpoint` |
| **Microsoft** | `MICROSOFT_SSO_CLIENT_ID`, `MICROSOFT_SSO_CLIENT_SECRET` (optional: `MICROSOFT_SSO_TENANT`, default `common`) | `https://<AUTH_DOMAIN>/realms/osmo/broker/microsoft/endpoint` |

- **Google:** Create OAuth 2.0 credentials in [Google Cloud Console](https://console.cloud.google.com/apis/credentials) (APIs & Services → Credentials → Create Credentials → OAuth client ID). Application type: Web application. Add the redirect URI above.
- **GitHub:** Create an OAuth App in [GitHub Developer Settings](https://github.com/settings/developers). Authorization callback URL = redirect URI above.
- **Microsoft (Azure AD):** Create an App registration in [Azure Portal](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade). Under Authentication, add a Web platform redirect URI as above. Use `MICROSOFT_SSO_TENANT=common` for multi-tenant or your tenant ID for single-tenant.

Re-run `04-deploy-osmo-control-plane.sh` after setting the env vars so the Keycloak realm gets the new IdPs. Users will see the corresponding buttons on the Keycloak login page.

## Enabling Nebius SSO

The integration in this repo uses **standard OIDC** (OpenID Connect) so that Keycloak can use Nebius SSO as an identity provider. If Nebius or your internal teams provide **Nebius-specific** integration (e.g. official Keycloak IdP guide, Terraform/Helm for SSO, or a custom OIDC profile), **use that first** and treat this section as a fallback or reference.

### Information to get from internal teams / Nebius SSO

Before configuring, obtain the following from your **Nebius SSO / corporate IdP team** or from internal documentation:

| What you need | Used for | Example / notes |
|---------------|----------|------------------|
| **OIDC Issuer URL** | Keycloak discovery (`.well-known/openid-configuration`) | e.g. `https://auth.nebius.com/realms/corporate` or tenant-specific URL |
| **Client registration** | How to create an OAuth/OIDC client for “Keycloak as a Relying Party” | Link to portal, API, or process; confirm it’s **OIDC** (not SAML-only) |
| **Client ID** | Keycloak IdP config | Issued when you register the client |
| **Client secret** | Keycloak IdP config | For confidential clients; where to rotate it if needed |
| **Redirect URI(s) allowed** | Must include Keycloak’s broker callback | Exact format and any wildcards; e.g. `https://<keycloak-host>/realms/osmo/broker/nebius-sso/endpoint` |
| **Scopes to request** | Usually `openid`; sometimes `email`, `profile`, `groups` | Confirm if `groups` (or another claim) is required for role mapping |
| **Claim / attribute for groups or roles** | Mapping users to OSMO Admin / User / Backend Operator | e.g. `groups`, `member_of`, `roles` – and whether it’s a string or list, and format (names vs IDs) |
| **Environment / tenant** | Correct issuer and endpoints | Prod vs non-prod; tenant or org ID if Nebius SSO is multi-tenant |
| **Network / allowlist** | Keycloak pod must reach Nebius SSO (discovery, token, JWKS) | Whether the cluster egress must be allowlisted; VPN or private endpoint requirements |

If Nebius provides a **Keycloak-specific** doc (e.g. “Integrating Keycloak with Nebius SSO”), it may also specify: exact issuer per environment, recommended client settings, and group/role claim names. Use that as the source of truth.

### Nebius SSO endpoints and client registration (from IAM)

Use the **prod** endpoint for auth unless you are testing against beta.

| Environment | Issuer URL | Discovery |
|-------------|------------|-----------|
| **Prod** | `https://auth.nebius.com` | `https://auth.nebius.com/.well-known/openid-configuration` |
| **Beta** | `https://auth.beta.nebius.ai` | `https://auth.beta.nebius.ai/.well-known/openid-configuration` |

- **OIDC client registration:** [Nebius IAM – OIDC client registration](https://docs.nebius.dev/en/iam/authentication/oidc-client/oidc-client-registration). Register a confidential client; set redirect URI to your Keycloak callback (e.g. `https://auth-osmo.<LB_IP_DASHED>.nip.io/realms/osmo/broker/nebius-sso/endpoint` for nip.io, or your domain). Use the **client ID** and **client secret** from that process. To change the redirect URI later: `npc iam oidc-client update <CLIENT_ID> --redirect-uris "https://..." --authorization-grant-types authorization_code --patch` (grant types required with `--patch`).
- **Groups claim:** Nebius SSO does not currently fill a groups claim. Role mapping (Admin / User / Backend Operator) cannot be done automatically from IdP groups. Assign users to the correct Keycloak realm groups (or roles) manually in Keycloak Admin after first login, or use a default role for all SSO users.
- **Egress:**
  - **Prod** (`auth.nebius.com`): Reachable on the public internet. If your cluster has outbound HTTPS, no allowlisting is required. Otherwise request a rule for this FQDN.
  - **Beta** (`auth.beta.nebius.ai`): Often requires a policy; the gateway may be exposed over IPv6. Request egress/allowlisting if needed.

### 1. Prerequisites

- Nebius SSO (OIDC) issuer URL, client ID, and client secret for an application that will act as the IdP for Keycloak (from the table above).
- Keycloak must be reachable over HTTPS with a stable hostname (TLS and `OSMO_INGRESS_HOSTNAME` or `KEYCLOAK_HOSTNAME` set).

### 2. Register Keycloak as a client in Nebius SSO

Registration is done via the **Identity Service gRPC API** (`nebius.iam.v1.OidcClientService`), not a web portal. Internal services use service-account credentials to call the API; alternatively, ask IAM in [#iam-support](https://nebius.enterprise.slack.com/archives/C062Z5WR62K) to create the client for you and provide the client ID and secret.

**Official doc:** [External OIDC Client Registration](https://docs.nebius.dev/en/iam/authentication/oidc-client/oidc-client-registration).

#### Option A: IAM creates the client for you

Post in #iam-support (e.g. @duty iam) and ask them to create a confidential OIDC client with the redirect URI and settings below. They will return a **client ID** and **client secret** (from `GenerateClientSecret`). Use those in [Configure deployment](#3-configure-deployment).

#### Option B: You call the gRPC API (programmatic)

You need access to the private IAM API (e.g. via `cpl.iam...`) and a **Project NID** as `parent_id`.

**Step 1: Create the OIDC client**

Call `Create(CreateOidcClientRequest)` with:

| Field | Value |
|-------|--------|
| `metadata.parent_id` | Your project NID (required) |
| `metadata.name` | Unique client name, e.g. `keycloak-osmo` |
| `spec.client_authentication_methods` | `["client_secret_basic"]` (recommended for server-side) |
| `spec.redirect_uris` | Exact callback URL, no trailing slash. Example (nip.io): `https://auth-osmo.89-169-122-246.nip.io/realms/osmo/broker/nebius-sso/endpoint`. Max 5 URIs. Update existing client: `npc iam oidc-client update <ID> --redirect-uris "https://..." --authorization-grant-types authorization_code --patch`. |
| `spec.scopes` | `["openid"]` — doc says at least one scope; if IAM requires a service-specific scope, ask in #iam-support. |
| `spec.authorization_grant_types` | `["authorization_code"]` (only supported grant type) |
| `spec.pkce_enabled` | Optional (e.g. `true`) |

The response includes the new client’s **client_id** (OIDC client NID). Save it.

**Step 2: Generate the client secret**

Call `GenerateClientSecret(GenerateOidcClientSecretRequest)` with the **client_id** from Step 1. The secret is returned **only once**; store it securely (e.g. in `keycloak-nebius-sso-secret`). You cannot retrieve it later.

**Step 3: Use the credentials**

Use the **client_id** from Create and the **client_secret** from GenerateClientSecret in [Configure deployment](#3-configure-deployment).

#### Auth endpoints (for reference)

- **Prod:** `https://auth.nebius.com` — authorize at `/oauth2/authorize`, token at `POST /oauth2/token`.
- **Beta:** `https://auth.beta.nebius.ai` — same paths.

Discovery (e.g. `/.well-known/openid-configuration`) is used by Keycloak to find these endpoints.

### 3. Configure deployment

Before running `04-deploy-osmo-control-plane.sh`:

```bash
# In defaults.sh or export in the shell (use prod for auth)
export NEBIUS_SSO_ENABLED="true"
export NEBIUS_SSO_ISSUER_URL="https://auth.nebius.com"
export NEBIUS_SSO_CLIENT_ID="<client-id-from-registration>"
export NEBIUS_SSO_CLIENT_SECRET="<client-secret-from-registration>"
```

Or store the client secret in Kubernetes and leave it out of the environment:

```bash
kubectl create secret generic keycloak-nebius-sso-secret \
  -n osmo --from-literal=client_secret="<client-secret>"
export NEBIUS_SSO_ENABLED="true"
export NEBIUS_SSO_ISSUER_URL="https://..."
export NEBIUS_SSO_CLIENT_ID="..."
# Do not set NEBIUS_SSO_CLIENT_SECRET; the script will use the secret above.
```

If you only updated the OIDC client redirect URI (e.g. to nip.io), reuse your existing `NEBIUS_SSO_CLIENT_SECRET` and `OSMO_POSTGRESQL_PASSWORD`; they do not change.

Then run:

```bash
./04-deploy-osmo-control-plane.sh
```

Keycloak will be configured with:

- **Nebius SSO** as an OIDC identity provider (alias `nebius-sso`), set as default for the browser flow.
- **No local test user** by default (no `osmo-admin` / `osmo-admin`).

### 4. Optional: Break-glass local user

To keep a local user for emergencies or for the backend script while still using Nebius SSO:

```bash
export CREATE_OSMO_TEST_USER="true"
# Then run 04-deploy-osmo-control-plane.sh
```

The `osmo-admin` user will be created and can be used for `OSMO_KC_ADMIN_USER` / `OSMO_KC_ADMIN_PASS` in `06-deploy-osmo-backend.sh`.

### 5. Group/role attribute (optional)

Nebius SSO does not currently fill a groups claim, so automatic role mapping from IdP groups is not available. Assign users to Keycloak realm groups **Admin**, **User**, or **Backend Operator** manually in Keycloak Admin (Realm osmo → Users → select user → Groups) after their first SSO login.

If you use a different IdP that sends a groups claim, set `NEBIUS_SSO_GROUP_ATTRIBUTE` to the claim name (e.g. `groups`). The setup job adds an IdP mapper for it; adjust mapping in Keycloak (Identity providers → nebius-sso → Mappers) as needed.

## Role and permission mapping

OSMO uses Keycloak **realm roles** and **groups**:

| Group / Role        | Purpose |
|---------------------|--------|
| **Admin** group     | Maps to `osmo-admin` and `osmo-user` roles (full access). |
| **User** group      | Maps to `osmo-user` (standard user). |
| **Backend Operator**| Maps to `osmo-backend` (for backend operator service token). |

- **Browser/API:** Keycloak includes these roles in the JWT `roles` claim; Envoy and OSMO enforce them.
- **Nebius SSO:** Groups claim is not currently filled; assign users to Admin, User, or Backend Operator manually in Keycloak after first login. For other IdPs that send a groups claim, use `NEBIUS_SSO_GROUP_ATTRIBUTE` and Keycloak IdP mappers.

## TLS and compatibility

- **TLS:** Keycloak must be exposed over HTTPS for production (OIDC redirects and cookies). Use the same TLS setup as the rest of OSMO (e.g. `03b-enable-tls.sh` and `KEYCLOAK_HOSTNAME` / `KEYCLOAK_TLS_SECRET_NAME`). The auth flow depends on TLS for the Keycloak hostname used in redirect URIs.
- **OSMO control plane:** Envoy sidecars use the same Keycloak realm and JWTs; no change needed when switching to Nebius SSO.
- **UI / APIs:** All use the same Keycloak session and JWT; Nebius SSO only changes how the user logs in (redirect to IdP instead of local form).

## Summary

| Goal | Action |
|------|--------|
| Use Nebius SSO as primary login | Set `NEBIUS_SSO_ENABLED=true`, `NEBIUS_SSO_ISSUER_URL`, `NEBIUS_SSO_CLIENT_ID`, and client secret (env or `keycloak-nebius-sso-secret`). Register Keycloak redirect URI in Nebius SSO. |
| Remove default username/password | Enable Nebius SSO as above; the test user is not created unless `CREATE_OSMO_TEST_USER=true`. |
| Map corporate groups to OSMO roles | For Nebius SSO: assign users to Admin/User/Backend Operator manually in Keycloak (groups claim not filled). For other IdPs: set `NEBIUS_SSO_GROUP_ATTRIBUTE` and configure mappers. |
| Backend operator with SSO | Create a local user (e.g. via `CREATE_OSMO_TEST_USER=true`) or a dedicated automation user, and set `OSMO_KC_ADMIN_USER` / `OSMO_KC_ADMIN_PASS` for `05-deploy-osmo-backend.sh`. |

For security review: ensure Nebius SSO client secret is stored in Kubernetes secret or a secure secret manager (e.g. MysteryBox), TLS is enabled for Keycloak, and redirect URIs in Nebius SSO are restricted to your Keycloak hostname.

---

## Ensure testing uses Nebius SSO (full flow)

If you ran `04-deploy-osmo-control-plane.sh` and saw **"OSMO API authentication is DISABLED"** or **"Keycloak internal-only"**, the UI/API are not yet behind Keycloak and Nebius SSO. To make testing use Nebius SSO end-to-end:

### Why auth was disabled

- The script enables **Envoy + Keycloak** only when Keycloak has an **external TLS ingress** (so the JWT issuer matches the browser).
- That requires a **TLS secret** for the Keycloak hostname (e.g. `auth-osmo.89-169-122-246.nip.io` or `auth-osmo.local`). The hostname must resolve from **inside the cluster** (so Envoy can fetch JWKS); use nip.io or a real domain, not only /etc/hosts. Without the TLS secret, Keycloak stays internal-only and Envoy is not enabled.

### What you need

1. **Env vars when running 04** (so Keycloak is configured with Nebius SSO and the correct hostname):
   ```bash
   export OSMO_INGRESS_HOSTNAME="osmo.local"
   export KEYCLOAK_HOSTNAME="auth-osmo.89-169-122-246.nip.io"   # or your domain; must resolve in cluster
   export OSMO_INGRESS_HOSTNAME="osmo.89-169-122-246.nip.io"
   export NEBIUS_SSO_ENABLED=true
   export NEBIUS_SSO_ISSUER_URL="https://auth.nebius.com"
   export NEBIUS_SSO_CLIENT_ID="<your-oidc-client-id>"
   export NEBIUS_SSO_CLIENT_SECRET="<your-oidc-client-secret>"
   # Optional: so 06-deploy-osmo-backend.sh can authenticate the backend operator (osmo-admin user)
   export CREATE_OSMO_TEST_USER="true"
   ```

2. **TLS secrets for Keycloak (and OSMO)** so the script creates the Keycloak ingress and turns on Envoy:
   - **Real domains:** Run `./03b-enable-tls.sh` (with DNS A records for `osmo.local` and `auth-osmo.local` pointing to the LoadBalancer). Let's Encrypt does not issue for `.local`, so for **.local** use the helper below.
   - **.local (e.g. osmo.local / auth-osmo.local):** Create self-signed TLS secrets, then re-run 04:
     ```bash
     cd applications/osmo/deploy/example/002-setup
     ./create-local-tls-secrets.sh
     # Then re-run 04 with the env vars above
     ./04-deploy-osmo-control-plane.sh
     ```

3. **Hosts file** so the browser can reach Keycloak and OSMO:
   ```bash
   # Replace <LB_IP> with your NGINX LoadBalancer IP (e.g. from kubectl get svc -n ingress-nginx ingress-nginx-controller)
   echo "<LB_IP>  osmo.local auth-osmo.local" | sudo tee -a /etc/hosts
   ```

4. **Test:** Open `https://osmo.local` (or `https://auth-osmo.local/realms/osmo/...`). You should be redirected to **Nebius SSO**, then back to OSMO after login. Accept the self-signed certificate warning in the browser if using the local script.

---

## Testing Nebius SSO (concise)

You do **not** need to deploy the full OSMO stack (backend, storage, GPU, etc.). You only need Keycloak plus the config job that adds the IdP.

### Minimal path to test login

1. **Prerequisites:** Infrastructure with PostgreSQL and NGINX Ingress (run `001-iac`, then `03-deploy-nginx-ingress.sh`). Set `OSMO_INGRESS_HOSTNAME` (and TLS if you want HTTPS).

2. **Run only the control-plane script** (this deploys Keycloak + runs the config job that adds Nebius SSO):
   ```bash
   cd deploy/example/002-setup
   export NEBIUS_SSO_ENABLED=true
   export NEBIUS_SSO_ISSUER_URL="https://<your-nebius-sso>/realms/<realm>"
   export NEBIUS_SSO_CLIENT_ID="<client-id>"
   export NEBIUS_SSO_CLIENT_SECRET="<client-secret>"
   ./04-deploy-osmo-control-plane.sh
   ```
   Skip 01, 02, 05, 06, 08 for this test.

3. **Confirm the IdP was created:** Check the config job logs:
   ```bash
   kubectl logs -n osmo job/keycloak-osmo-setup | grep -A2 "Step 4c"
   ```
   You should see: `Nebius SSO IdP created (HTTP 201)` (or 204).

4. **Test redirect to Nebius SSO:** Open in a browser:
   - Keycloak login for `osmo` realm:  
     `https://<AUTH_DOMAIN>/realms/osmo/protocol/openid-connect/auth?client_id=osmo-browser-flow&redirect_uri=...&response_type=code&scope=openid`
   - Or simply open the OSMO UI URL (e.g. `https://<OSMO_INGRESS_HOSTNAME>`) and follow the login redirect.
   You should be sent to Nebius SSO (corporate login), then back to Keycloak and OSMO after success.

### If you already have a full OSMO deployment

Set the same env vars, create `keycloak-nebius-sso-secret` if you use a secret instead of `NEBIUS_SSO_CLIENT_SECRET`, then **re-run** `04-deploy-osmo-control-plane.sh`. The script is idempotent: it re-imports the realm and runs the config job again, which adds the Nebius SSO IdP. Then repeat steps 3–4 above.

### Sanity check without a real Nebius SSO

To verify only that the IdP is registered (login will fail at the IdP):

- Use any valid OIDC issuer URL and client credentials (e.g. a test OAuth app). After `04-deploy-osmo-control-plane.sh`, run step 3; if you see `Nebius SSO IdP created (HTTP 201)`, the wiring is correct. Opening the login URL should then redirect to that issuer.

---

## How do you know you’re connected correctly to Nebius SSO?

You’re connected correctly when the **full login flow** works end-to-end: you’re sent to Nebius SSO, sign in with corporate credentials, and land back in OSMO (or Keycloak) with a session. Nothing else is a full proof.

### 1. Definitive check: complete login

1. Open the OSMO Web UI or the Keycloak login URL for the `osmo` realm (e.g. `https://<AUTH_DOMAIN>/realms/osmo/...`).
2. You should see the Keycloak login page with a **"Nebius SSO" button** (Keycloak does not auto-redirect; `authenticateByDefault` is `false` so local login remains available alongside SSO).
3. Click the **Nebius SSO** button and sign in with your Nebius/corporate credentials.
4. You should be **redirected back** to Keycloak and then to OSMO with a valid session (no error, UI loads).

If all four steps succeed, the connection to Nebius SSO is correct (issuer, client id/secret, and redirect URI all match and work).

### 2. What goes wrong when something is misconfigured

| What you see | Likely cause |
|--------------|--------------|
| **"Missing 'code_challenge' for public client"** at auth.nebius.com | Nebius SSO requires PKCE. The deploy script sets `pkceEnabled=true` and `pkceMethod=S256` on the Keycloak IdP. If you already created the IdP, **update it**: Keycloak Admin → Realm osmo → Identity providers → nebius-sso → set **PKCE** to **On** and **PKCE method** to **S256**, Save. Or delete the nebius-sso IdP and re-run 04 so it is recreated with PKCE. |
| Browser goes to Nebius SSO but then shows **redirect_uri_mismatch** (or similar) | The redirect URI in the Nebius OIDC client must exactly match the Keycloak callback (scheme, host, path, no trailing slash). Update it: `npc iam oidc-client update <CLIENT_ID> --redirect-uris "https://<KEYCLOAK_HOST>/realms/osmo/broker/nebius-sso/endpoint" --authorization-grant-types authorization_code --patch`. |
| After logging in at Nebius SSO you get an **error page** from Keycloak (e.g. invalid client, unauthorized) | Wrong **client ID** or **client secret** in Keycloak (or in the Nebius SSO client). Re-check `NEBIUS_SSO_CLIENT_ID` and the secret (env or `keycloak-nebius-sso-secret`). |
| Keycloak login page appears **without** redirecting to Nebius SSO (e.g. local login form only) | IdP not created or not default: check job logs for “Nebius SSO IdP created”; in Keycloak Admin → Realm osmo → Identity providers, confirm “nebius-sso” exists and is enabled. |
| Redirect to Nebius SSO **never happens** (error or Keycloak error page first) | **Issuer URL** wrong or Nebius SSO unreachable from Keycloak (e.g. discovery fails). Check `NEBIUS_SSO_ISSUER_URL`, network, and Keycloak logs. |
| Browser returns to `https://<OSMO_HOST>/oauth2/callback` and then shows a **403** / "Access was denied" page | `oauth2-proxy` rejected the callback because the brokered Keycloak ID token either did not present `email_verified=true` or did not contain an `email` claim at all. The deploy script now adds `--insecure-oidc-allow-unverified-email=true` and, for Nebius SSO, `--oidc-email-claim=preferred_username` to the OSMO `oauth2-proxy` sidecars. **Re-run 05** so service/router/UI pick up the updated sidecar args. |
| Login works, but opening `https://<OSMO_HOST>` again a few minutes later shows **JWT expired** | The browser session cookie outlived the embedded Keycloak access token. In the shipped realm, Keycloak issues 5-minute browser access tokens, while the old `oauth2-proxy` config refreshed only every hour. The deploy script now sets `sidecars.oauth2Proxy.cookieRefresh=4m` so the session refreshes before token expiry. **Re-run 05** so service/router/UI pick up the new refresh interval. |
| Clicking **Sign out** just logs you straight back in, including in an existing incognito window | The Web UI cleared only the local `oauth2-proxy` cookie and did not terminate the upstream Keycloak SSO session. The deploy script now sets `sidecars.oauth2Proxy.oidcEndSessionUrl` and `--whitelist-domain=<AUTH_DOMAIN>` so `/signout` performs federated logout through Keycloak. **Re-run 05** so the Web UI picks up the new logout wiring. |
| **CLI opens "Device Login" page and asks for a code**, but the terminal never showed a code / **"Invalid code"** | The CLI uses the **device flow**; the code should appear in the terminal. If it doesn’t or you prefer the redirect-style flow, use password login instead: `osmo login https://<OSMO_HOST> --method dev --username osmo-admin` (requires the test user; set `CREATE_OSMO_TEST_USER=true` before 04). |
| **"Successfully logged in" but next command returns login HTML / "Error decoding JSON"** (e.g. `osmo pool list`) | The API request is unauthenticated (token not sent or lost on redirect). **Workaround:** use port-forward so the CLI talks to localhost and no redirect occurs: `kubectl port-forward -n osmo svc/osmo-service 8080:80`, then `osmo login http://localhost:8080 --method dev --username osmo-admin` and run your command again. If it works, the CLI may be dropping the token when the server redirects to Keycloak. |
| **"Could not send authentication request"** / **"Param was null"** in Keycloak logs | Keycloak could not build the IdP auth URL. The deploy script now sets **explicit** `authorizationUrl` and `tokenUrl` for Nebius SSO (`.../oauth2/authorize`, `.../oauth2/token`) so discovery is not required. If you still see this, **re-run 04** (or delete job `keycloak-osmo-setup` and re-run 04) so the IdP is recreated with these URLs. Otherwise check egress and [Troubleshooting](#troubleshooting-could-not-send-authentication-request). |

### 3. Optional: verify in Keycloak Admin

- Log in to Keycloak Admin: `https://<AUTH_DOMAIN>/admin` (admin / &lt;KEYCLOAK_ADMIN_PASSWORD&gt;).
- Realm **osmo** → **Identity providers** → **nebius-sso**.
- Confirm it’s there and **Enabled**. This only shows that the IdP was created; it does **not** prove that Nebius SSO accepts the redirect URI or client credentials. Only the full login flow (section 1) does that.

### 4. Troubleshooting: "Could not send authentication request"

This message means Keycloak (running in the cluster) failed to send the user to the IdP—usually because it cannot reach the Nebius SSO discovery or auth endpoints.

**Step 1 – Keycloak logs (exact error):**

```bash
kubectl logs -n osmo -l app.kubernetes.io/name=keycloak --tail=200 2>&1 | grep -i -E "nebius|identity|provider|discovery|auth\.nebius|error|exception"
```

Look for connection refused, timeout, TLS, or discovery errors.

**Step 2 – Can the cluster reach Nebius SSO?**

From your machine (cluster egress may differ):

```bash
curl -s -o /dev/null -w "%{http_code}" https://auth.nebius.com/.well-known/openid-configuration
# Expect 200
```

From inside the cluster (same network as Keycloak):

```bash
kubectl run curl-nebius --rm -it --restart=Never --image=curlimages/curl -- \
  curl -s -w "\nHTTP_CODE:%{http_code}\n" https://auth.nebius.com/.well-known/openid-configuration | tail -5
```

If this fails (timeout, connection refused, non-200), the cluster cannot reach Nebius SSO. Fix egress/firewall/DNS so the Keycloak pod can reach `auth.nebius.com` on HTTPS.

**Step 3 – Issuer URL**

- Use exactly **`https://auth.nebius.com`** for prod (no trailing slash, no path unless Nebius docs say otherwise).
- In Keycloak Admin → Realm osmo → Identity providers → nebius-sso → check **Discovery endpoint** or **Issuer** matches that URL.

**Step 4 – Re-run 04 with correct env**

If you fixed issuer or network, re-run so the IdP config is updated. You can also edit the IdP in Keycloak Admin (change issuer, save) and retry without re-running 04.
