from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Literal

import plaid
from plaid.api import plaid_api

_log = logging.getLogger("plaid_mcp")
if not _log.handlers:
    _log.addHandler(logging.StreamHandler(sys.stderr))
    _log.setLevel(logging.INFO)

_ENV_MAP = {
    "production": plaid.Environment.Production,
    "sandbox": plaid.Environment.Sandbox,
}


class SecretStr:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretStr('<redacted>')"

    def __str__(self) -> str:
        return "<redacted>"

    def __format__(self, spec: str) -> str:
        return "<redacted>"


def load_tokens() -> dict[str, SecretStr]:
    out: dict[str, SecretStr] = {}
    prefix = "PLAID_TOKEN_"
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            out[key[len(prefix):]] = SecretStr(value)
    return out


def build_api() -> plaid_api.PlaidApi:
    client_id = os.environ["PLAID_CLIENT_ID"]
    secret = os.environ["PLAID_SECRET"]
    env_name = os.environ.get("PLAID_ENV", "production").lower()
    host = _ENV_MAP.get(env_name, plaid.Environment.Production)
    config = plaid.Configuration(
        host=host,
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(config))


def map_plaid_error(exc: Exception, institution: str | None) -> dict:
    trace_id = str(uuid.uuid4())
    body: dict = {}
    try:
        parsed = json.loads(getattr(exc, "body", "") or "{}")
        body = parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        body = {}
    code = body.get("error_code") or body.get("error_type") or "UNKNOWN"
    message = body.get("error_message") or "Plaid call failed."
    request_id = body.get("request_id")
    _log.warning(
        "plaid_error trace_id=%s request_id=%s code=%s",
        trace_id,
        request_id,
        code,
    )
    err: dict = {"code": code, "message": message, "trace_id": trace_id}
    if institution:
        err["institution"] = institution
    return {"error": err}


# ---------------------------------------------------------------------------
# Per-Item lazy health cache
# ---------------------------------------------------------------------------

from plaid.api.plaid_api import PlaidApi  # noqa: E402 – after package import
from plaid.exceptions import ApiException  # noqa: E402
from plaid.model.item_get_request import ItemGetRequest  # noqa: E402
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest  # noqa: E402
from plaid.model.country_code import CountryCode  # noqa: E402

HealthStatus = Literal[
    "healthy",
    "re_auth_required",
    "pending_expiration",
    "item_locked",
    "no_accounts",
    "unknown_error",
]


@dataclass
class ItemHealth:
    env_key: str
    status: HealthStatus
    institution_id: str | None
    institution_name: str | None
    reason: str | None = None


_health_cache: dict[str, tuple[ItemHealth, float]] = {}
_CACHE_TTL_SEC = 300

_ERROR_TO_STATUS: dict[str, HealthStatus] = {
    "ITEM_LOGIN_REQUIRED": "re_auth_required",
    "PENDING_EXPIRATION": "pending_expiration",
    "ITEM_LOCKED": "item_locked",
    "NO_ACCOUNTS": "no_accounts",
}


def get_item_health(api, env_key: str, token: SecretStr) -> ItemHealth:
    now = time.time()
    cached = _health_cache.get(env_key)
    if cached and (now - cached[1]) < _CACHE_TTL_SEC:
        return cached[0]
    try:
        item_resp = api.item_get(ItemGetRequest(access_token=token.reveal())).to_dict()
        item = item_resp.get("item") or {}
        ins_id = item.get("institution_id")
        ins_name: str | None = None
        if ins_id:
            try:
                ins_resp = api.institutions_get_by_id(
                    InstitutionsGetByIdRequest(
                        institution_id=ins_id,
                        country_codes=[CountryCode("US")],
                    )
                ).to_dict()
                ins_name = (ins_resp.get("institution") or {}).get("name")
            except Exception:
                ins_name = None
        err = item.get("error")
        if err:
            status: HealthStatus = _ERROR_TO_STATUS.get(
                err.get("error_code"), "unknown_error"
            )
            reason = err.get("error_code")
        else:
            status = "healthy"
            reason = None
        health = ItemHealth(env_key, status, ins_id, ins_name, reason)
    except ApiException as e:
        reason_body: dict = {}
        try:
            parsed = json.loads(getattr(e, "body", "") or "{}")
            if isinstance(parsed, dict):
                reason_body = parsed
        except (ValueError, TypeError):
            reason_body = {}
        reason = reason_body.get("error_code") or f"HTTP {getattr(e, 'status', '?')}"
        health = ItemHealth(env_key, "unknown_error", None, None, reason=reason)
    _health_cache[env_key] = (health, now)
    return health


def all_items(api) -> list[tuple[str, SecretStr, ItemHealth]]:
    out: list[tuple[str, SecretStr, ItemHealth]] = []
    for env_key, token in load_tokens().items():
        out.append((env_key, token, get_item_health(api, env_key, token)))
    return out


# ---------------------------------------------------------------------------
# Response shaping helpers
# ---------------------------------------------------------------------------


def make_handle(institution: str, subtype: str, mask: str | None) -> str:
    """Generate a deterministic, stable handle for an account.

    Converts institution/subtype/mask to lowercase, strips non-alphanumerics,
    and joins with underscores. Example: "Chase" + "checking" + "1234" -> "chase_checking_1234".
    """
    def norm(s: str | None) -> str:
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    parts = [norm(institution), norm(subtype)]
    if mask:
        parts.append(mask)
    return "_".join(p for p in parts if p)


def shape_account(raw: dict, institution: str | None) -> dict:
    """Shape a raw Plaid account dict into a trimmed, normalized response.

    - Drops PII fields like persistent_account_id.
    - Flattens nested balances into a single balance dict with currency.
    - Adds a stable handle for account reference across turns.
    """
    bals = raw.get("balances") or {}
    return {
        "handle": make_handle(
            institution or "",
            raw.get("subtype") or raw.get("type") or "",
            raw.get("mask"),
        ),
        "account_id": raw.get("account_id"),
        "institution": institution,
        "name": raw.get("name"),
        "official_name": raw.get("official_name"),
        "mask": raw.get("mask"),
        "type": raw.get("type"),
        "subtype": raw.get("subtype"),
        "balance": {
            "current": bals.get("current"),
            "available": bals.get("available"),
            "limit": bals.get("limit"),
            "currency": bals.get("iso_currency_code") or bals.get("unofficial_currency_code"),
        },
    }


def shape_transaction(raw: dict) -> dict:
    """Shape a raw Plaid transaction dict into a trimmed, normalized response.

    - Prefers personal_finance_category (primary/detailed) over legacy category.
    - Extracts merchant_name or falls back to name.
    - Includes pending status and currency.
    """
    pfc = raw.get("personal_finance_category") or {}
    return {
        "transaction_id": raw.get("transaction_id"),
        "account_id": raw.get("account_id"),
        "date": str(raw.get("date")) if raw.get("date") else None,
        "amount": raw.get("amount"),
        "currency": raw.get("iso_currency_code"),
        "merchant": raw.get("merchant_name") or raw.get("name"),
        "name": raw.get("name"),
        "category": {"primary": pfc.get("primary"), "detailed": pfc.get("detailed")},
        "pending": bool(raw.get("pending")),
    }


def shape_holding(raw_holding: dict, securities: dict[str, dict]) -> dict:
    """Shape a raw Plaid holding dict, joining with security data.

    - Looks up security metadata (symbol, name, type) by security_id.
    - Flattens institution_value -> market_value, institution_price -> price.
    - Handles missing security gracefully (returns None for symbol/name/type).
    """
    sec = securities.get(raw_holding.get("security_id"), {})
    return {
        "account_id": raw_holding.get("account_id"),
        "symbol": sec.get("ticker_symbol"),
        "name": sec.get("name"),
        "type": sec.get("type"),
        "quantity": raw_holding.get("quantity"),
        "cost_basis": raw_holding.get("cost_basis"),
        "market_value": raw_holding.get("institution_value"),
        "price": raw_holding.get("institution_price"),
        "currency": raw_holding.get("iso_currency_code"),
    }
