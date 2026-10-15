# Bunnings MCP Server

A FastMCP server for accessing Bunnings APIs — product search, stores, pricing, stock, cart, and shopping lists.

Complete and utter vibe-coded nonsense.Has worked for me, but may not for you. In fact I'd be surprised.

## Features

- **Product Search** — Search the Bunnings catalog by keyword
- **Product Details** — Full product info including description, features, images
- **Pricing** — Current prices for any product
- **Stock & Fulfillment** — In-store, Click & Collect, and delivery availability
- **Store Locator** — List all stores with addresses, hours, and services
- **Aisle/Bay Locations** — Find where products are in the store
- **Cart Management** — Add, update, remove items (requires user auth)
- **Shopping Lists** — Create and manage project lists (requires user auth)

## Quick Start

### Install

```bash
pip install -e .
```

### Run

```bash
bunnings-mcp
```

### Docker

```bash
docker build -t bunnings-mcp .
docker run -p 8000:8000 bunnings-mcp
```

## Tools

### Product Tools (No Auth Required)

| Tool | Description |
|------|-------------|
| `search_products` | Search products by keyword |
| `get_product` | Get full product details by SKU |
| `get_product_price` | Get current price |
| `get_product_stock` | Check stock/fulfillment |
| `get_product_location` | Get aisle/bay location |
| `browse_category` | Browse by category path |

### Store Tools (No Auth Required)

| Tool | Description |
|------|-------------|
| `list_stores` | List all stores with details |

### Cart Tools (User Auth Required)

| Tool | Description |
|------|-------------|
| `get_cart` | Get current cart |
| `add_to_cart` | Add item to cart |
| `update_cart_item` | Update item quantity |
| `remove_from_cart` | Remove item from cart |

### Shopping List Tools (User Auth Required)

| Tool | Description |
|------|-------------|
| `list_shopping_lists` | List all project lists |
| `create_shopping_list` | Create a new list |
| `get_shopping_list` | Get list with items |
| `add_to_shopping_list` | Add item to list |
| `remove_from_shopping_list` | Remove item from list (by SKU or entry id) |
| `rename_shopping_list` | Rename a list |
| `update_shopping_list_item` | Change an item's quantity in a list |
| `move_shopping_list_items` | Move item(s) between lists |
| `set_active_shopping_list` | Set the active list |
| `add_shopping_list_to_cart` | Add every item in a list to the cart |
| `delete_shopping_list` | Delete a list |

Verified request shapes, gotchas, and the known (unexposable) endpoints:
[`BunningsProjectLists.md`](BunningsProjectLists.md).

## Authentication

Product search, details, pricing, stock, and store locations use a **guest token** that is automatically minted via Bunnings' public OAuth2 flow — no login required.

Cart and shopping list tools require an **authenticated user session**. Bunnings uses Okta with
mandatory MFA, so the server cannot log in on your behalf — you log in once in your browser,
then the server keeps the session alive **indefinitely** using the refresh token.

### ⚠️ The cookies MUST come from an isolated login

Bunnings' refresh token is **single-use and rotates on every use** — only the newest value works.
If the browser session you copied the cookies from keeps using that login, it will rotate the
refresh token out from under the MCP and the server will be **logged out** (and vice versa).

So the cookies you give the MCP must come from a login that is **not used interactively
afterwards**:

- Use a **private/incognito window** (or a separate browser profile) you don't browse with.
- Copy the cookies, then **close that window** and don't use that session again.
- Don't copy from your everyday tab — if you keep browsing with it, the MCP will keep going stale.

### Setup (one-time)

1. Open a **private/incognito window** and log in to
   [bunnings.com.au](https://www.bunnings.com.au) or [bunnings.co.nz](https://www.bunnings.co.nz),
   including MFA.
2. Open DevTools → Application → Storage → Cookies → the Bunnings domain.
3. Copy **both** cookie values:
   - `UserAuthentication` (a JWT, starts with `eyJ`)
   - `UserAuthenticationRefresh` (looks like 64 hex chars + `-1`)
4. Call the `login` tool for instructions, then `complete_login` with both values:
   ```
   complete_login(user_token="eyJ...", refresh_token="ABC123...-1")
   ```
5. **Close the private/incognito window.**

That's it. The MCP caches the pair and **automatically renews** the 10-minute access token via the
Bunnings IdentityServer's public OIDC `refresh_token` grant (client `budp_user_au_code`, no secret).
The refresh token rotates on every use and the server persists the new one immediately, so the
chain keeps going until the refresh token is revoked (typically weeks/months of idle-free use).

Use `check_login_status` to see whether a session is active and auto-renewal is enabled.

### If the session goes stale

When a session dies because the refresh chain was rotated elsewhere, the server does **not**
silently report "not logged in". Errors carry a machine-readable `error_code` (and a `hint`):

| `error_code` | Meaning | Fix |
|--------------|---------|-----|
| `refresh_token_rejected` | The refresh cookie had already been used (rotated elsewhere) — almost always the browser you copied from | Re-run the one-time setup, from an **isolated** login this time |
| `access_expired_no_refresh` | Access token expired and no refresh cookie was stored | Provide `UserAuthenticationRefresh` too |
| `refresh_unreachable` | Couldn't reach the Bunnings token endpoint (transient) | Retry shortly — the session is left intact |
| `not_logged_in` | No session cached at all | Run the one-time setup |
| `refresh_failed` / `refreshed_token_rejected` | The refresh cookie was rejected or the renewed session failed validation | Re-run the one-time setup |

`check_login_status` returns the same `error_code`/`hint` when a session can't be used.

Full auth details, findings, and failed approaches: [`BunningsAuth.md`](BunningsAuth.md).

## Testing

Standalone scripts (no pytest needed) that drive the real MCP server through the FastMCP
in-memory client:

| Script | Scope | Needs network + session |
|--------|-------|--------------------------|
| `test_mcp_suite.py` | **Live end-to-end**: exercises every tool (including optional params), verifies each mutation with a read-back MCP call, then restores all state | Yes |
| `test_server_tools.py` | Offline regression: drives the tools via the MCP client with the API layer monkeypatched | No |
| `test_session_conflict.py` | Offline: stale/conflicting refresh-chain handling — error codes, hints, no needless rotation | No |
| `test_auth_flow.py` | Auth lifecycle: login, refresh rotation, auto-refresh, process restart, negatives | Yes |
| `test_session_refresh.py` | Manual probe of the OIDC refresh-token chain (needs fresh browser cookies) | Yes |

```bash
python3 test_server_tools.py      # 20 offline checks, no login needed
python3 test_session_conflict.py  # 39 offline checks, no login needed
python3 test_mcp_suite.py         # 32 live checks (drives every tool)
```

`test_mcp_suite.py` is safe to run against your real account: it creates and deletes
throwaway project lists, adds/removes only the cart entries it creates, restores the active
list, and backs up/restores the cached session file around the `logout` test. It prints a
per-case PASS/FAIL report and an end-state check (cart, lists, session) and exits non-zero
on any failure.

## API Research

This server is built from:
- [Bunnings Developer Portal](https://developer.live.bunnings.com.au/) — official API docs
- [Unofficial API notes](https://github.com/thecolab-ai/.skills/blob/main/skills/bunnings/references/api-notes.md) — reverse-engineered endpoints
- Direct analysis of bunnings.com.au website traffic

### Key Endpoints

- `POST /_apis/v1/coveo/search` — Product search (Coveo)
- `GET /_apis/v1/products/{sku}?fields=FULL` — Product details
- `GET /_apis/v2/products/{sku}/priceInfo` — Pricing
- `POST /_apis/v2/products/{sku}/fulfillment` — Stock/fulfillment
- `GET /_apis/v1/item-api/locations` — Aisle/bay locations
- `GET /_apis/v1/stores/country/{AU|NZ}?fields=FULL` — Store list
- `/_apis/v1/basket` — Cart operations (auth required)
- `/_apis/v1/customer-management/users/wishlist` — Project lists (auth required; see [`BunningsProjectLists.md`](BunningsProjectLists.md))

## License

MIT
