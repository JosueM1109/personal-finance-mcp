# Deployment

This server runs anywhere Python 3.11+ and HTTP work. Options below, shortest to most featured.

## Local only

Fine if you only need to use it from the same machine:

```bash
source .venv/bin/activate
PORT=8000 python server.py
```

Point your MCP client at `http://localhost:8000/mcp`. No auth gate needed — nothing outside your machine can reach it.

## Docker

A minimal `Dockerfile` ships with the repo:

```bash
docker build -t personal-finance-mcp .
docker run --rm -p 8000:8000 --env-file .env personal-finance-mcp
```

`link_helper.py` is deliberately **not** copied into the image — it's a local-only tool for obtaining access tokens and must never run in a deployment. If you expose the container publicly, put an auth proxy in front (Caddy + OAuth, Cloudflare Access, nginx with basic auth, etc.).

## Prefect Horizon

The author's setup. Free tier with scale-to-zero, OAuth 2.1 built in, ~$0 recurring cost. Trade-off: 20-60s cold start after idle.

1. Push the repo to a **private** GitHub repo (your deployment will bake env vars into a dashboard, so a private source repo is a nice extra layer).
2. Go to https://horizon.prefect.io and sign in with GitHub.
3. **New Server → Connect repo → personal-finance-mcp**.
4. Configure:
   - **Entrypoint:** `server.py:mcp`
   - **Environment variables:** every line from your local `.env`, one per row:
     - `PLAID_CLIENT_ID`
     - `PLAID_SECRET`
     - `PLAID_ENV=production`
     - Every `PLAID_TOKEN_<NAME>` line
     - `HORIZON=1` (belt-and-braces guard that blocks `link_helper.py` from ever running on the deployment)
   - **Access control:** enable **OAuth 2.1**, restrict to your own email.
5. Click **Deploy**. Wait ~60s for the first build.
6. Copy the deployment URL (e.g. `https://<name>.fastmcp.app/mcp`).
7. Add to your MCP client:
   ```bash
   claude mcp add --transport http personal-finance https://<name>.fastmcp.app/mcp
   ```
   Claude Code will prompt you to complete the OAuth flow in a browser. Sign in with the email you restricted in step 4.

## Any other host

Anything that runs Python + HTTP works: Fly.io, Railway, a Raspberry Pi behind Tailscale, a VPS with systemd. Requirements:

- Python 3.11+
- Env vars from `.env.example` (plus `HORIZON=1` to keep `link_helper.py` disabled)
- Persistent HTTPS endpoint at `/mcp`
- Auth gate in front unless on a private network
- `server.py` reads `PORT` from env, which most PaaS hosts inject automatically

## Adding a bank after the initial setup

1. On your laptop: `source .venv/bin/activate && uvicorn link_helper:app --port 8765`
2. Open http://localhost:8765, click **Link a bank**, complete the flow
3. Copy the new `PLAID_TOKEN_<NAME>=access-prod-...` from the terminal
4. Add the new env var to your deployment (Horizon env panel, `docker run --env-file`, etc.)
5. Redeploy or wait for the next request — most platforms pick up new env vars automatically
6. (Optional) paste the line into your local `.env` too if you want to test against the real token locally

No code change or git push needed.

## Handling `ITEM_LOGIN_REQUIRED` (re-auth)

When a bank's MFA token expires or you change your password, Plaid returns `ITEM_LOGIN_REQUIRED`. You'll see it in `get_institutions_status()` or as a warning on any tool call.

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

Copy the returned `link_token` into a quick HTML snippet, or modify `INDEX_HTML` in `link_helper.py` to accept an `update_access_token` query param. Complete Plaid Link → your existing access token stays the same, just re-enabled on Plaid's side. No env var changes needed.
