# Architecture

## What each file does

### `server.py`

The MCP server. Defines `mcp = FastMCP("personal-finance-mcp")` at module scope (so FastMCP-aware hosts can import `server.py:mcp`) and registers 9 read-only tools: `list_accounts`, `get_balances`, `get_transactions`, `get_recurring_transactions`, `get_liabilities`, `get_investment_holdings`, `get_investment_transactions`, `get_institutions_status`, `search_transactions`.

Every tool iterates every healthy Plaid Item, calls the corresponding Plaid endpoint, shapes the response, and returns `{"<data>": [...], "warnings": [...]}`. Partial failures (one bank unhealthy, others fine) surface as warnings rather than errors, so a single broken connection doesn't break every query.

### `plaid_client.py`

Plaid SDK wrapper.

- **`SecretStr`** wraps access tokens and overrides `__repr__`, `__str__`, and `__format__` so tokens can't leak via logs or f-strings.
- **`load_tokens()`** scans `os.environ` for `PLAID_TOKEN_*` keys.
- **`get_item_health()`** calls Plaid's `item_get` + `institutions_get_by_id` with a 5-minute cache to distinguish healthy Items from those needing re-auth.
- **`shape_account`**, **`shape_transaction`**, and **`shape_holding`** trim raw Plaid responses to small, normalized shapes that MCP clients reason over well.
- **`map_plaid_error`** converts `plaid.ApiException` into `{"error": {"code", "message", "institution?", "trace_id"}}` and logs the Plaid `request_id` to stderr for correlation.

### `link_helper.py`

Local-only FastAPI app for the one-time Plaid Link flow. Guarded at import time: if `HORIZON=1` is set, it `sys.exit`s before loading anything else. Creates a link token, serves a tiny HTML page with Plaid Link JS, receives the `public_token` callback, exchanges it, and **prints** the access token to the terminal — never writes to disk, never includes it in the HTTP response body.

## `/transactions/get` vs `/transactions/sync`

This server uses Plaid's `/transactions/get` endpoint with offset pagination rather than `/transactions/sync` with persistent cursors.

`/sync` is more efficient per-request but requires storing a cursor per Item. A scale-to-zero deployment can't keep per-Item cursors in memory, and persisting them would require external state (Redis, SQLite, etc.) — out of scope for a personal read-only server.

Plaid's 2-year lookback window is enforced: if you ask for a start date older than ~24 months, it gets clipped and a `WINDOW_CLIPPED` warning is attached to the response.

## Warnings, not exceptions

Every tool returns `{"<data>": [...], "warnings": [...]}`. When one Plaid Item errors (re-auth needed, product not enabled, rate limit, etc.), the other Items still succeed and the broken one becomes a warning entry like:

```json
{
  "institution": "Chase",
  "code": "ITEM_LOGIN_REQUIRED",
  "message": "the login details of this item have changed",
  "trace_id": "..."
}
```

This keeps queries useful when one bank is temporarily broken — common during password changes or after a bank-side MFA rotation.
