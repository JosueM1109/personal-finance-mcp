# personal-finance-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP](https://img.shields.io/badge/MCP-read--only-brightgreen.svg)](https://modelcontextprotocol.io)

> **Unofficial.** This project is not affiliated with, endorsed by, or sponsored by Plaid Inc. "Plaid" is a trademark of Plaid Inc. This is a self-hosted client that talks to Plaid's API using credentials you supply.

A self-hosted, read-only MCP server that connects your banks, credit cards, loans, and brokerage accounts (via Plaid) to an MCP client like Claude Code. Ask questions about your own finances in plain English — no third-party aggregator (Monarch, Mint, etc.) involved.

## What you can ask

- "What's my total balance across all accounts?"
- "Show me transactions over $100 in the last 30 days."
- "Which subscriptions am I still paying for?"
- "How much did I spend on groceries last month?"
- "Any bank that needs re-authentication?"

Example session (illustrative):

```
you    : What did I spend on groceries last month?
claude : [calls get_transactions]
         $487.23 across 14 transactions. Top merchants:
         Whole Foods ($198), Trader Joe's ($156), Safeway ($89).

you    : Any subscriptions I'm still paying for?
claude : [calls get_recurring_transactions]
         7 active recurring outflows totaling $142/mo:
         Netflix ($15.99), Spotify ($11.99), NYT ($4), ...
```

## Tools

All 9 tools are read-only. Each returns `{<data>: [...], "warnings": [...]}` so one broken bank doesn't break the whole query.

| Tool                          | What it does                                                         |
| ----------------------------- | -------------------------------------------------------------------- |
| `list_accounts`               | Every account across every linked bank, with balances                |
| `get_balances`                | Live current + available balances (optionally filtered by account)   |
| `get_transactions`            | Transactions in a date range (up to 2 years back)                    |
| `search_transactions`         | Keyword search across merchant / name / counterparty                 |
| `get_recurring_transactions`  | Detected recurring inflow + outflow streams                          |
| `get_liabilities`             | Credit cards, student loans, mortgages with APRs and payment details |
| `get_investment_holdings`     | Current holdings with symbol + security metadata                     |
| `get_investment_transactions` | Buy / sell / dividend history in a date range                        |
| `get_institutions_status`     | Health of each linked bank (surfaces re-auth needs)                  |

## Quickstart

Requires Python 3.11+, a Plaid account (free Trial plan), and an MCP client.

### 1. Plaid setup

1. Sign up at https://dashboard.plaid.com/signup → choose the **Trial** plan (free, 10 Items).
2. **Team Settings → Products**: enable **Transactions**, **Liabilities**, **Investments**.
3. **Team Settings → API**: copy your `client_id` and production `secret`.

### 2. Install

```bash
git clone https://github.com/JosueM1109/personal-finance-mcp.git
cd personal-finance-mcp
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in PLAID_CLIENT_ID and PLAID_SECRET
pytest -v              # sanity check
```

### 3. Link each bank

Run once per bank you want to connect:

```bash
uvicorn link_helper:app --port 8765
```

Open http://localhost:8765, click **Link a bank**, complete Plaid Link. The terminal prints a line like `PLAID_TOKEN_CHASE=access-prod-xxx...` — paste it into `.env` and repeat for each bank.

### 4. Run it

```bash
python server.py   # serves on http://localhost:8000/mcp
```

### 5. Add to Claude Code

```bash
claude mcp add --transport http personal-finance http://localhost:8000/mcp
```

Try "list my accounts" to confirm.

## Deployment

For a deployment you can use from anywhere:

- **Docker** (included): `docker build -t personal-finance-mcp . && docker run --rm -p 8000:8000 --env-file .env personal-finance-mcp`
- **Any Python host** (Fly.io, Railway, Raspberry Pi + Tailscale, a VPS): set the env vars from `.env.example`, expose `/mcp` over HTTPS, gate it with auth.
- **Prefect Horizon** (what the author uses — $0 recurring cost): see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full walkthrough.

**Gate the endpoint.** An exposed MCP endpoint with your tokens leaks every linked account. Use OAuth 2.1, Cloudflare Access, or bind to a private network only.

## Security

- **Single-tenant.** One deployment per person. Don't share.
- **Read-only.** No tool mutates state at any institution. Don't add any that do.
- **Tokens live in env vars**, never on disk. `.env` is gitignored.
- **You own Plaid compliance.** You're the Plaid customer under your own account.

Before each deploy:

- [ ] `.env` never committed: `git log --all -- .env` returns nothing
- [ ] No real tokens in history: `git log -S'access-prod-' --all` returns only placeholders
- [ ] Auth gate in front of the MCP endpoint (or localhost-only)
- [ ] `HORIZON=1` (or similar) set in deployment env, blocking `link_helper.py` there
- [ ] Check `get_institutions_status()` every few weeks for re-auth needs

## Troubleshooting

**Tool returns empty despite real data.** Plaid products weren't enabled when you linked the bank. Re-link with Transactions + Liabilities + Investments active. The tool surfaces `PRODUCTS_NOT_SUPPORTED` in `warnings` when this is the cause.

**`get_institutions_status()` shows `re_auth_required`.** The bank's Plaid session expired. Run `link_helper.py` in update mode — your existing access token stays the same. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#handling-item_login_required-re-auth).

**Plaid Link shows a bank as "unsupported" (common with Amex).** Usually an `INSTITUTION_REGISTRATION_REQUIRED` issue — OAuth banks need per-institution registration in the Plaid dashboard first. See [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

More issues: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Architecture

- [server.py](server.py) — FastMCP server, 9 read-only tools.
- [plaid_client.py](plaid_client.py) — Plaid SDK wrapper: `SecretStr` token redaction, 5-minute per-Item health cache, response shaping, structured error mapping.
- [link_helper.py](link_helper.py) — Local-only FastAPI app for Plaid Link. Refuses to run if `HORIZON=1` is set.

Deeper dive (including why `/transactions/get` over `/transactions/sync`): [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Scope is deliberately narrow: read-only, single-tenant, Plaid-backed.
