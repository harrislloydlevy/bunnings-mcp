# Bunnings Authentication — Full Technical Documentation

> Living document. Update whenever new auth findings occur. **Read this before working on auth.**

## The Two Identity Systems

| System | URL | Role |
|--------|-----|------|
| **Okta** | `login.bunnings.com.au` | Login frontend (username/password/MFA), issues `sessionToken` |
| **IdentityServer** | `authorisation.api.bunnings.com.au` | OIDC provider, issues the actual access/refresh tokens |

### IdentityServer OIDC Discovery
`GET https://authorisation.api.bunnings.com.au/.well-known/openid-configuration`
- issuer: `https://bunnings.com.au/`
- authorization_endpoint: `/connect/authorize`
- token_endpoint: `/connect/token`
- refresh capability: standard OIDC (token endpoint responds to `grant_type=refresh_token`)

### Clients
| Client ID | Type | Notes |
|-----------|------|-------|
| `budp_user_au_code` | **public** | Used for user login. Token endpoint accepts it WITHOUT client_secret (returns `invalid_grant` for bad codes, NOT `invalid_client`). Issuer of UserAuthentication JWT. |
| `budp_guest_user_au` | **public** | Guest token flow (works, implemented in `auth.py`) |
| `0oa6rg2svokEfnLKF3l6` | Okta client | Used for the browser authorize redirect. Our direct token exchange attempts at Okta return `401 invalid_client` (likely needs server secret). |

## The Real Login Flow (from HAR analysis)

The web app does **NOT** use a simple OAuth2 code flow. The actual flow:

1. **Okta authn API** (password + MFA):
   ```
   POST https://login.bunnings.com.au/api/v1/authn
   → {"status": "MFA_REQUIRED", "stateToken": "...", factors: [email factor]}
   POST https://login.bunnings.com.au/api/v1/authn/factors/{factorId}/verify  (challenge)
   POST https://login.bunnings.com.au/api/v1/authn/factors/{factorId}/verify  (passCode)
   → {"status": "SUCCESS", "sessionToken": "20111GTQDP5PO2..."}
   ```
2. **Browser redirect** through Okta authorize → callback:
   ```
   GET https://www.bunnings.com.au/auth/callback/retail-login
       ?code=E68D29DC...-1          (64-hex + "-1" format!)
       &state=...
       &session_state=...           (Okta session_state)
   ```
3. **Server-side exchange** (not visible in HAR): the Bunnings backend exchanges
   the Okta code/token for IdentityServer tokens. The `code` format matches the
   `UserAuthenticationRefresh` cookie format — both are IdentityServer artifacts.
4. **Cookies set**:
   - `UserAuthentication` → 10-min JWT (RS256, issuer `https://bunnings.com.au/`)
   - `UserAuthenticationRefresh` → 64-hex + `-1` (rotating refresh token, server-side exchange)
   - `bunnings-auth-idToken` → OIDC id_token

## Cookie Behaviour (critical findings)

### UserAuthentication (access token)
- RS256 JWT, 10-minute TTL, claims include:
  - `client_id: budp_user_au_code`
  - `sub` (user id), `b-id` (bunnings id), `given_name`, `preferred_username`
  - scopes: `chk:exec cm:access ecom:access ... offline_access`
- Sent by browser as cookie (also accepted as `Authorization: Bearer` by APIs? — cookie is what works)

### UserAuthenticationRefresh (refresh token)
- Format: `HEX64-1` e.g. `FD745B996E406C471190BE1BE067665135595B71ED031871DBCB9764B593568D-1`
- **ROTATES on every use** (single-use). The browser demonstrates this by showing a
  different value on successive requests, because it refreshes on each cycle.
- Rotation is driven by the **OIDC refresh grant** (see "Session Keep-Alive" below),
  *not* by ordinary API requests — regular API calls do not return a new
  `UserAuthentication` cookie.
- Persist the rotated value immediately after every refresh, or the chain breaks.

## API Endpoints (verified working)

Auth header: `Cookie: UserAuthentication=<jwt>` (+ `GuestAuthentication=<guest>` for guest)
Also send: `clientId: mHPVWnzuBkrW7rmt56XGwKkb5Gp9BJMk`, `country: AU`, `currency: AUD`, `locale: en_AU`, `locationCode`, `stream: RETAIL`

| Endpoint | Method | Purpose | Body |
|----------|--------|---------|------|
| `/_apis/v1/customer-management/users/wishlist?sortOrder=default&fields=SLIM` | GET | List wishlists | — |
| `/_apis/v1/customer-management/users/wishlist` | POST | Add item to wishlist | `{"code":"<sku>","list":"","name":"My Project List","productCategory":"","productName":"","productValue":0,"quantity":"1","wishlistId":"<id>","wishlistName":"My Project List"}` |
| `/_apis/v1/customer-management/users/wishlist/{id}` | GET | Wishlist detail | — |
| `/_apis/v1/customer-management/users/wishlist/{id}/setActive` | POST | Set active wishlist | — |
| `/_apis/v1/carts?oldCartId=&fields=LITE&cartCreate=false` | POST | Get/create cart | — |
| `/_apis/v1/carts/{cartId}?fields=LITE` | GET | Get cart | — |
| `/_apis/v1/carts/{cartId}/entries?code=<sku>&qty=1&fields=LITE&validateStock=false` | POST | Add to cart | — |
| `/_apis/v1/stores/checkActiveStore?currentStore=<id>&defaultStore=<id>&radius=50000` | GET | Store check | — |
| `/apis/auth/session` | GET | Validate session | — |
| `/_apis/v1/customer-management/users` | GET | User info | — |

## Session Keep-Alive — CONFIRMED WORKING ✅

**Confirmed mechanism: standard OIDC refresh_token grant at the IdentityServer.**
No client secret is needed because `budp_user_au_code` is a public client.

```
POST https://authorisation.api.bunnings.com.au/connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
&client_id=budp_user_au_code
&refresh_token=<HEX64-1>
```

Response:
```json
{
  "access_token": "<new 10-min JWT>",
  "refresh_token": "<NEW HEX64-1>",
  "expires_in": 600,
  "scope": "... offline_access ...",
  "token_type": "Bearer"
}
```

### Key behaviours (all verified empirically)

1. **Single-use rotation.** Each refresh returns a NEW refresh token and invalidates the
   old one. The response MUST be persisted atomically or the chain breaks
   (`invalid_grant` on reuse). The code does this inside `refresh_user_token()`.
2. **The chain is indefinite.** Refreshing repeatedly (10+ times) works; each link
   rotates cleanly. There is no observed absolute lifetime (until revocation).
3. **Refreshed access token works for API calls** — identical claims to the browser
   token (`aud`, `scope`, `client_id`, `sid` all match), only `exp/iat/nbf/jti` differ.
4. **The API requires `GuestAuthentication` AND `UserAuthentication` together.**
   Sending only `UserAuthentication` returns **401**. This was the cause of an early
   false-negative during testing.
5. **Cookie-rotation-on-API-call (sliding session) is NOT the mechanism.** Regular API
   calls do not set `UserAuthentication`; renewal is done explicitly via the refresh grant.

### Why the browser never re-logs-in
The web app performs the same refresh grant (or an equivalent BFF exchange) in the
background, rotating the cookie on each cycle. The browser works purely because it keeps
refreshing — not because requests themselves extend the session.

### WARNING — cookie chain conflicts (rotating refresh token + shared login)

The refresh token is **single-use and rotates on every use**; only the newest value works.
If two clients share the **same** login, each one's rotation invalidates the other's stored
token and the loser gets `invalid_grant` → logged out. This is *the* failure mode to design
around, and it is asymmetric — whoever refreshes last wins.

The cookies given to the MCP must therefore come from an **isolated login that will not be
used interactively afterwards**:

1. Log in in an **incognito/private window** (or a separate browser profile), including MFA.
2. Copy `UserAuthentication` + `UserAuthenticationRefresh`.
3. **Close the window** and do not use that session again — then call `complete_login`.

Copying from an everyday tab you keep browsing with will work briefly and then break.
Note that using the MCP itself does not conflict: it is *one* holder of the chain (it
persists each rotated token immediately under a cross-process lock).

#### How the code detects a dead chain
`_refresh_token_grant()` now distinguishes the token endpoint's response:

- `invalid_grant` → **the chain was consumed elsewhere** (the shared/rotated case above).
- `network_error` / `http_<code>` → transient or unexpected; the session is **kept**, not cleared.

An `invalid_grant` marks the cached session (`refresh_rejected`) instead of silently deleting
it, so later calls return a stable error code + hint rather than a bare "not logged in":

| `error_code` | Cause |
|--------------|-------|
| `refresh_token_rejected` | Refresh token already rotated elsewhere (the shared-login case) |
| `access_expired_no_refresh` | Access token expired, no refresh cookie stored |
| `refresh_unreachable` | Token endpoint unreachable (transient; session kept intact) |
| `not_logged_in` | No cached session |
| `refresh_failed` / `refreshed_token_rejected` | Refresh rejected / renewed session failed validation |

Also fixed: `_read_cached_user_token()` was subtracting `TOKEN_REFRESH_SKEW` twice (once when
writing `cache_expires_at`, again when reading), which gave the ~10-minute access token a
**zero-second** cache window and forced a refresh (and rotation) on *every* authenticated call.
That needless rotation made shared-chain conflicts arrive almost immediately.

### Implementation (in `bunnings_mcp/auth.py`)
1. `set_user_token(cfg, user_token, refresh_token)` — validates via the **wishlist API**
   (not `/apis/auth/session`, which 404s server-side) and caches both tokens.
2. `get_user_token(cfg)` — returns a valid cached access token; if expired, transparently
   refreshes and returns the renewed one. Delegates to `user_session_status()`, which also
   reports *why* there is no token (never-logged-in vs dead chain vs network error).
3. `refresh_user_token(cfg)` / `refresh_user_token_with_reason(cfg)` — perform the grant above
   and persist the rotated pair; the latter also returns a failure reason.
4. `session_error_status(reason)` — maps a failure reason to the customer-facing
   `{error_code, error, hint}` dict used by the tools.
5. `get_user_refresh_token(cfg)` — exposes whether auto-renewal is enabled.
6. `client.api_request()` retries on 401/403; if the refresh fails it raises
   `BunningsAPIError` carrying the same `error_code` instead of a raw 401.

## Failed Approaches (don't retry these)

1. ❌ **Direct Okta token exchange** (`POST login.bunnings.com.au/oauth2/v1/token` with code) → `401 invalid_client` — needs server-side secret.
2. ❌ **Bunnings IdentityServer authorize flow** (`/connect/authorize` with our own PKCE) → redirects to `/home/error` — redirect_uri not registered for this flow.
3. ❌ **Calling the callback URL directly** (`/auth/callback/retail-login?code=...`) → `loginCallbackError=disabled` — requires browser context (state cookie, Okta session cookies).
4. ❌ **Refresh token at token endpoint with STALE tokens** → `invalid_grant`.
   **RESOLVED**: stale/rotated tokens are single-use; a fresh chain works fine (see above).
5. ❌ **Calling `/apis/auth/session` from the server** → HTTP 404 (works only in the
   browser). Use the wishlist API for server-side validation instead.
6. ❌ **API call with only the `UserAuthentication` cookie** → HTTP 401.
   Must also send `GuestAuthentication`.

## Current Status

- [x] Guest token flow (product search etc.) — WORKING
- [x] Cookie-based user API calls (wishlist add, cart) — WORKING
- [x] Correct endpoints documented and implemented in `client.py`
- [x] Direct `refresh_token` grant at IdentityServer — **CONFIRMED WORKING**
- [x] Auto-refresh on token expiry (`get_user_token`) — CONFIRMED WORKING
- [x] Seamless login: user pastes both cookies ONCE; server renews indefinitely
- [ ] Long-term: measure real-world refresh-chain lifetime (weeks/months?)

## Key Code Locations

- `bunnings_mcp/auth.py` — guest token minting, `set_user_token`, `get_user_token`,
  `refresh_user_token`, `get_user_refresh_token`
- `bunnings_mcp/client.py` — `_api_headers` (adds `UserAuthentication` + `GuestAuthentication`
  cookies), all API calls, auto-retry with refresh on 401
- `bunnings_mcp/server.py` — FastMCP tools: `login`, `complete_login`, `check_login_status`
- `test_session_refresh.py` — script that proves the refresh grant works
- `/tmp/bunnings_cookies.json` — local test file for cookie pairs (not committed)
