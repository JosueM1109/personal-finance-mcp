"""Local-only Plaid Link helper.

Run one-time per bank to obtain a long-lived access token. Never deploy.

Usage:
    cp .env.example .env            # fill PLAID_CLIENT_ID / PLAID_SECRET
    source .venv/bin/activate
    uvicorn link_helper:app --port 8765

Then open http://localhost:8765 in your browser, click "Link a bank",
complete the Plaid Link flow, and paste the printed env var line into
your local .env and your deployment's env settings.

For re-auth (ITEM_LOGIN_REQUIRED):
    curl -X POST localhost:8765/create-link-token \\
      -H "content-type: application/json" \\
      -d '{"update_access_token": "access-prod-EXISTING"}'
    # then open the returned link_token in the browser widget
"""
from __future__ import annotations

import os
import sys

# Defense-in-depth: refuse to run on Horizon (deployment env).
if os.environ.get("HORIZON"):
    sys.exit("link_helper.py must not run on Horizon. Run locally only.")

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_update import LinkTokenCreateRequestUpdate
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products

from plaid_client import build_api

app = FastAPI(title="Personal Finance MCP — Link Helper")
api = build_api()


class CreateReq(BaseModel):
    update_access_token: str | None = None


@app.post("/create-link-token")
def create_link_token(req: CreateReq) -> dict:
    if req.update_access_token:
        body = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id="personal-user"),
            client_name="Personal Finance MCP",
            country_codes=[CountryCode("US")],
            language="en",
            access_token=req.update_access_token,
            update=LinkTokenCreateRequestUpdate(account_selection_enabled=False),
        )
    else:
        body = LinkTokenCreateRequest(
            user=LinkTokenCreateRequestUser(client_user_id="personal-user"),
            client_name="Personal Finance MCP",
            products=[Products("transactions")],
            optional_products=[
                Products("liabilities"),
                Products("investments"),
            ],
            country_codes=[CountryCode("US")],
            language="en",
        )
    return api.link_token_create(body).to_dict()


class ExchangeReq(BaseModel):
    public_token: str


@app.post("/exchange")
def exchange(req: ExchangeReq) -> dict:
    resp = api.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=req.public_token)
    ).to_dict()
    access_token = resp["access_token"]
    item_id = resp["item_id"]

    item = api.item_get(ItemGetRequest(access_token=access_token)).to_dict().get("item", {}) or {}
    ins_id = item.get("institution_id")
    ins_name = "UNKNOWN"
    if ins_id:
        try:
            ins_name = api.institutions_get_by_id(
                InstitutionsGetByIdRequest(
                    institution_id=ins_id,
                    country_codes=[CountryCode("US")],
                )
            ).to_dict()["institution"]["name"]
        except Exception:
            ins_name = "UNKNOWN"

    env_suffix = "".join(ch for ch in ins_name.upper() if ch.isalnum())
    env_key = f"PLAID_TOKEN_{env_suffix}" if env_suffix else "PLAID_TOKEN_UNKNOWN"

    print("=" * 60, flush=True)
    print(f"Institution: {ins_name}", flush=True)
    print(f"Item ID:     {item_id}", flush=True)
    print("Add this to your .env (local) and Horizon env (prod):", flush=True)
    print(f"  {env_key}={access_token}", flush=True)
    print("Do NOT commit this line.", flush=True)
    print("=" * 60, flush=True)

    return {"institution": ins_name, "item_id": item_id, "env_key": env_key}


INDEX_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Plaid Link (local)</title></head>
<body style="font-family:system-ui;max-width:640px;margin:2rem auto;padding:0 1rem">
  <h1>Personal Finance MCP &mdash; Link a bank</h1>
  <p>Click the button, complete Plaid Link in the popup, then check this terminal for the access token.</p>
  <button id="link" style="padding:.75rem 1.5rem;font-size:1rem">Link a bank</button>
  <pre id="out" style="margin-top:1rem;white-space:pre-wrap"></pre>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    document.getElementById('link').onclick = async () => {
      const r = await fetch('/create-link-token', {
        method: 'POST',
        headers: {'content-type':'application/json'},
        body: JSON.stringify({}),
      });
      const data = await r.json();
      if (!data.link_token) {
        document.getElementById('out').textContent = 'Error: ' + JSON.stringify(data);
        return;
      }
      const handler = Plaid.create({
        token: data.link_token,
        onSuccess: async (public_token) => {
          const ex = await fetch('/exchange', {
            method: 'POST',
            headers: {'content-type':'application/json'},
            body: JSON.stringify({public_token}),
          });
          document.getElementById('out').textContent =
            'Done. Check the terminal for your access token line.\\n\\n' + await ex.text();
        },
        onExit: (err) => {
          if (err) document.getElementById('out').textContent = 'Exit: ' + JSON.stringify(err);
        },
      });
      handler.open();
    };
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML
