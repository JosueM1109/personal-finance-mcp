# personal-finance-mcp

> **Unofficial.** This project is not affiliated with, endorsed by, or sponsored by Plaid Inc. "Plaid" is a trademark of Plaid Inc. This is a self-hosted client that talks to Plaid's API using credentials you supply.

A self-hosted, read-only Plaid MCP server for personal use. It connects to an MCP client like Claude Code and exposes your own bank accounts, credit cards, loans, and brokerage holdings so you can ask questions like "what did I spend on groceries last month?" or "which subscriptions are still active?" — without giving your data to a third-party aggregator (Monarch, Mint, etc.). You own the code, the Plaid tokens, and the deployment.

## Security model

This is the short version. See the [Security checklist](#security-checklist) for the full list.

- **Single-tenant by design.** Run one instance per person. Do not share a deployment across users.
- **Gate the endpoint.** Put OAuth 2.1 (or similar auth) in front of the MCP HTTP endpoint, or bind it to localhost only. An exposed endpoint with valid tokens leaks every linked account.
- **Tokens live in env vars.** Never commit `.env`. The server never writes credentials to disk.
- **Read-only.** No tool in this server initiates transfers, creates accounts, or mutates state at any institution. Do not add such tools.
- **You own Plaid compliance.** You are the Plaid customer under your own account; you're responsible for complying with Plaid's ToS for the data you access.

## Architecture at a glance

- **`server.py`** — FastMCP server with 9 read-only tools. Exposes `mcp` at module scope so hosts that import `server.py:mcp` (e.g. FastMCP-aware PaaS) can wire up OAuth and HTTP transport.
- **`plaid_client.py`** — Plaid SDK wrapper: `SecretStr` token redaction, lazy 5-minute per-Item health cache, response shaping (trims Plaid's raw JSON into small, normalized dicts), structured error mapping.
- **`link_helper.py`** — Local-only FastAPI app run one time per bank to complete Plaid Link and get access tokens. Guarded from running in deployment environments via `HORIZON=1`.
- **Tokens live in env vars only.** `PLAID_TOKEN_<INSTITUTION>` per linked bank. Nothing is ever written to disk by the deployed server.

## Setup

Do these in order. Everything in steps 1-7 happens locally; step 8 onward depends on how you deploy (see [Deployment](#deployment)).

### 1. Create a Plaid account

1. Go to https://dashboard.plaid.com/signup and sign up.
2. When asked for a plan, choose **Trial** (free, 10 Items max — plenty for personal use).

### 2. Enable products in the Plaid dashboard

In the Plaid dashboard, **Team Settings → Products**, enable:
- **Transactions**
- **Liabilities**
- **Investments**

If you only care about spending (no brokerage), you can skip Investments — but it's free to enable.

### 3. Fill out the Link use case

**Link → Link customization** (or "Use case"). Select "Personal financial management / budgeting" (or the closest match). Fill in any required fields (app name, logo, etc.) — they don't matter much for personal use.

### 4. Copy your API credentials

**Team Settings → API** (or "Keys"). Copy:
- `client_id`
- **Production `secret`** (not sandbox — you want real bank data)

### 5. Clone the repo and set up locally

```bash
git clone https://github.com/<you>/personal-finance-mcp.git
cd personal-finance-mcp
cp .env.example .env
```

Edit `.env`:

```dotenv
PLAID_CLIENT_ID=<paste from step 4>
PLAID_SECRET=<paste from step 4>
PLAID_ENV=production
# PLAID_TOKEN_* lines get filled in by step 7 below
```

Create a venv and install:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 6. Run the tests (sanity check)

```bash
pytest -v
```

All tests should pass. This confirms the environment is set up correctly before linking banks.

### 7. Link each bank with `link_helper.py`

For **every bank** you want to connect (Chase, Amex, Fidelity, whatever):

```bash
uvicorn link_helper:app --port 8765
```

Then:
1. Open http://localhost:8765 in your browser.
2. Click **Link a bank**.
3. In the Plaid Link widget, pick your bank and log in.
4. Switch back to the terminal — you'll see a banner like:

   ```
   ============================================================
   Institution: Chase
   Item ID:     abc123...
   Add this to your .env (local) and your deployment env (prod):
     PLAID_TOKEN_CHASE=access-prod-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   Do NOT commit this line.
   ============================================================
   ```
5. Paste the `PLAID_TOKEN_<NAME>=...` line into your local `.env`.
6. Stop uvicorn (Ctrl-C), restart, and repeat for the next bank.

## Deployment

Several options — pick whatever fits your infrastructure. All options share the same requirements: Python 3.11+, the env vars from `.env.example`, and a persistent HTTPS endpoint. An auth gate (OAuth 2.1, Cloudflare Access, etc.) is strongly recommended unless you bind to localhost only.

### Option A: Run locally

Simplest option if you only want to use this from the same machine that runs it:

```bash
source .venv/bin/activate
PORT=8000 python server.py
```

Then point your MCP client at `http://localhost:8000/mcp`. No auth gate needed because nothing outside your machine can reach it.

### Option B: Docker

A minimal `Dockerfile` ships with the repo:

```bash
docker build -t personal-finance-mcp .
docker run --rm -p 8000:8000 --env-file .env personal-finance-mcp
```

`link_helper.py` is intentionally **not** copied into the image — it's a local-only tool for obtaining access tokens and must never run in a deployment. Put an auth proxy (e.g. Caddy + OAuth, Cloudflare Access, a reverse-proxy with basic auth) in front of the container if you expose it publicly.

### Option C: Prefect Horizon (what the author uses)

1. Push the repo to a private GitHub repo.
2. Go to https://horizon.prefect.io and sign in with GitHub.
3. **New Server → Connect repo → personal-finance-mcp**.
4. Configure:
   - **Entrypoint:** `server.py:mcp`
   - **Environment variables:** paste everything from your local `.env`, one var per row:
     - `PLAID_CLIENT_ID`
     - `PLAID_SECRET`
     - `PLAID_ENV=production`
     - Every `PLAID_TOKEN_<NAME>` line
     - Add `HORIZON=1` (belt-and-braces guard that stops `link_helper.py` from ever running on the deployment).
   - **Access control:** enable **OAuth 2.1** and restrict to your own email.
5. Click **Deploy**. Wait for the green status (~60 seconds on first build).
6. Copy the deployment URL — it looks like `https://<name>.fastmcp.app/mcp`.

Horizon's free tier scales to zero between requests, which gives a ~$0 recurring cost but adds a 20-60 second cold start on the first call after idle time.

### Option D: Any other host that runs Python + HTTP

Anything that can run a Python 3.11+ HTTP service works: Fly.io, Railway, a Raspberry Pi behind Tailscale, a VPS with systemd, etc. Requirements:

- Set all the env vars from `.env.example` (plus `HORIZON=1` if you want the `link_helper.py` guard active).
- Expose a persistent HTTPS endpoint at `/mcp`. The server reads `PORT` from env (see `server.py`), which most PaaS hosts inject automatically.
- Put auth in front of the endpoint unless it's only reachable on a private network.

## Add the server to your MCP client

For Claude Code:

```bash
claude mcp add --transport http personal-finance https://<your-deployment>/mcp
```

If the endpoint is OAuth-gated, Claude Code will prompt you to complete the flow in a browser.

Try it out with:
- "List my accounts"
- "What's my current checking balance?"
- "Show me all transactions over $100 in the last 30 days"
- "Which subscriptions am I paying for?"
- "Any bank that needs re-authentication?"

## What each file does

- **`server.py`** — The MCP server. Defines `mcp = FastMCP("personal-finance-mcp")` at module scope and registers 9 read-only tools: `list_accounts`, `get_balances`, `get_transactions`, `get_recurring_transactions`, `get_liabilities`, `get_investment_holdings`, `get_investment_transactions`, `get_institutions_status`, `search_transactions`. Each tool iterates every healthy Plaid Item, calls the corresponding Plaid endpoint, shapes the response, and returns a dict with `{"<data>": [...], "warnings": [...]}`.
- **`plaid_client.py`** — Plaid SDK wrapper. `SecretStr` wraps access tokens and overrides `__repr__` / `__str__` / `__format__` so tokens can't leak via logs or f-strings. `load_tokens()` scans `os.environ` for `PLAID_TOKEN_*` keys. `get_item_health()` calls Plaid's `item_get` + `institutions_get_by_id` with a 5-minute cache to know which banks are healthy vs need re-auth. `shape_account`, `shape_transaction`, and `shape_holding` trim raw Plaid responses to small, normalized shapes that Claude reasons over well. `map_plaid_error` converts `plaid.ApiException` into `{"error": {"code", "message", "institution?", "trace_id"}}` and logs the Plaid `request_id` to stderr for correlation.
- **`link_helper.py`** — Local-only FastAPI app for the one-time Plaid Link flow. Guarded at import time: if `HORIZON=1` is set, it `sys.exit`s before loading anything else. Creates a link token, serves a tiny HTML page with Plaid Link JS, receives the `public_token` callback, exchanges it, and **prints** the access token to the terminal (never writes to disk, never includes it in the HTTP response body).
- **`requirements.txt`** — Pinned dependency versions (`fastmcp>=3.2.4`, `plaid-python>=39.1.0`, `fastapi`, `uvicorn`, `pytest`).
- **`tests/`** — Unit tests with mocked Plaid calls. `test_plaid_client.py` covers the wrapper internals; `test_server.py` exercises each tool's happy/error paths; `test_link_helper.py` covers the deployment guard and HTML index.

## Transactions: `/transactions/get` (date range), not `/transactions/sync`

This server uses Plaid's `/transactions/get` endpoint with offset pagination rather than `/transactions/sync` with persistent cursors. A scale-to-zero deployment can't keep per-Item cursors in memory, and persisting them would require external state (Redis, SQLite, etc.) — out of scope for a personal read-only server. Plaid's 2-year lookback window is enforced: if you ask for a start date older than ~24 months, it gets clipped and a warning is attached.

## Adding a new bank later

1. Locally: `source .venv/bin/activate && uvicorn link_helper:app --port 8765`.
2. Open http://localhost:8765, click **Link a bank**, complete the flow.
3. Copy the new `PLAID_TOKEN_<NAME>=access-prod-...` line from the terminal.
4. Add the new env var to your deployment (Horizon env panel, `docker run --env-file`, etc.).
5. Redeploy or wait for the next request — most platforms pick up new env vars automatically.
6. (Optional) paste the line into your local `.env` too if you want to test against the real token locally.

## Handling `ITEM_LOGIN_REQUIRED` (a bank needs re-auth)

When a bank's MFA token expires or you change your password, Plaid returns `ITEM_LOGIN_REQUIRED`. You'll see it in `get_institutions_status()` or as a warning on any tool call. To fix:

```bash
source .venv/bin/activate
uvicorn link_helper:app --port 8765
```

In another terminal:

```bash
curl -X POST http://localhost:8765/create-link-token \
  -H 'content-type: application/json' \
  -d '{"update_access_token": "access-prod-YOUR-EXISTING-TOKEN"}'
```

Copy the returned `link_token` into a quick HTML snippet, or modify the `INDEX_HTML` in `link_helper.py` to accept an `update_access_token` query param. Complete Plaid Link → **your existing access token stays the same**, it's just re-enabled on Plaid's side. No env var changes needed.

## Security checklist

Run through this before and after each deploy:

- [ ] `.env` is gitignored and was never committed. Verify: `git log --all -- .env` returns nothing.
- [ ] No access token appears in any commit. Verify: `git log -S'access-prod-' --all` returns nothing.
- [ ] The MCP endpoint has an auth gate (OAuth 2.1, Cloudflare Access, etc.) OR is bound to localhost / a private network.
- [ ] Plaid dashboard **Team Settings** shows only your email under team members.
- [ ] The deployment treats `PLAID_SECRET` and `PLAID_TOKEN_*` as secrets (masked previews, no logs).
- [ ] `HORIZON=1` is set in the deployment environment (blocks `link_helper.py` from ever running there).
- [ ] Ask your MCP client `get_institutions_status()` every few weeks to surface re-auth needs proactively.

## Troubleshooting

**1. Deployment boots but tools return "No such file or directory" or crash on startup.**
→ Missing env var. Check deployment logs. `PLAID_CLIENT_ID` and `PLAID_SECRET` are required. The server imports fine with zero `PLAID_TOKEN_*` vars (you'd just get empty responses), but the Plaid credentials must be present.

**2. A tool returns an empty list even though the account has data.**
→ That Item's Plaid products don't include the one the tool needs. `get_liabilities` requires the `liabilities` product to have been enabled at Link time; `get_investment_holdings` requires `investments`. If you linked the bank before enabling the product in the Plaid dashboard, you need to **unlink and re-link** that bank with all three products active. The tool will surface `PRODUCTS_NOT_SUPPORTED` in `warnings` when this happens.

**3. First MCP call after a long idle period takes 20-60 seconds.**
→ Scale-to-zero cold start on your host (common with Horizon free tier and similar PaaS). The first call builds the container; subsequent calls are fast (the 5-minute health cache also warms up after the first call). Not a bug.

**4. `get_institutions_status()` shows `re_auth_required` on a bank.**
→ Plaid's `ITEM_LOGIN_REQUIRED`. The bank's session with Plaid has expired (password changed, MFA token rotated, bank forced a re-auth). Follow the update-mode flow in the "Handling `ITEM_LOGIN_REQUIRED`" section above. Your access token stays the same after re-auth.

**5. Plaid Link shows a bank as "unsupported" (common with American Express).**
→ Plaid Link filters institutions by the `products` array on the Link token. If `investments` is required, any bank without brokerage (Amex, most retail banks) is hidden. `link_helper.py` requests only `transactions` as required and puts `liabilities` + `investments` in `optional_products`, so all banks are reachable and each Item is initialized with whatever products it actually supports.

**6. Link flow errors with `INSTITUTION_REGISTRATION_REQUIRED`.**
→ OAuth institutions (Amex, Chase, Wells Fargo, Capital One, BoA, Citi, Schwab, PNC, etc.) require per-institution registration in the Plaid dashboard before you can create Items. Go to https://dashboard.plaid.com/activity/status/oauth-institutions, find the bank, and register. Most are one-click auto-registrations that take a few hours (up to 24). Schwab and PNC have extra questions. Non-OAuth banks (smaller banks, most credit unions, fintechs like Ally/SoFi/Marcus) don't need this. While you wait, you can develop against sandbox: flip `.env` to `PLAID_ENV=sandbox` and use credentials `user_good` / `pass_good` in Plaid Link.

**7. `fastmcp inspect server.py:mcp` fails locally, or a tool import path error shows up after `pip install`.**
→ Plaid SDK version mismatch. Confirm `plaid-python==39.1.0` is installed: `.venv/bin/pip show plaid-python`. If something else is installed, `pip install -r requirements.txt --force-reinstall`. If the error is in FastMCP's tool registration, confirm `fastmcp==3.2.4`: both libraries pin major versions in `requirements.txt` to prevent silent drift.
