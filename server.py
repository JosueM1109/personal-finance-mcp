from __future__ import annotations

import os
from datetime import date, timedelta

from fastmcp import FastMCP
from plaid.exceptions import ApiException
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.investments_transactions_get_request import InvestmentsTransactionsGetRequest
from plaid.model.investments_transactions_get_request_options import InvestmentsTransactionsGetRequestOptions
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest

from plaid_client import (
    ItemHealth,
    all_items,
    build_api,
    map_plaid_error,
    shape_account,
    shape_holding,
    shape_transaction,
)

mcp = FastMCP("personal-finance-mcp")


def _warning_from_health(h: ItemHealth) -> dict:
    return {
        "institution": h.institution_name or h.env_key,
        "status": h.status,
        "reason": h.reason,
    }


def _list_accounts_impl() -> dict:
    """List every account across all linked Items, with balances.

    Returns:
        {"accounts": [...], "warnings": [...]}. Warnings describe Items that
        are unhealthy (re-auth required, etc.) or hit API errors on this call.
    """
    api = build_api()
    accounts: list[dict] = []
    warnings: list[dict] = []
    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        try:
            resp = api.accounts_get(
                AccountsGetRequest(access_token=token.reveal())
            ).to_dict()
            for raw in resp.get("accounts", []):
                accounts.append(shape_account(raw, health.institution_name))
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})
    return {"accounts": accounts, "warnings": warnings}


list_accounts = mcp.tool(
    name="list_accounts",
    annotations={"readOnlyHint": True, "title": "List Accounts"},
)(_list_accounts_impl)


def _get_balances_impl(account_ids: list[str] | None = None) -> dict:
    """Get live current + available balances for accounts.

    Args:
        account_ids: Optional filter. When omitted, returns balances for every
            account across every healthy Item. When provided, only matching
            accounts are returned; Items that don't own any of the IDs emit a
            warning (INVALID_ACCOUNT_ID) rather than failing the call.

    Returns:
        {"accounts": [...], "warnings": [...]}.
    """
    api = build_api()
    accounts: list[dict] = []
    warnings: list[dict] = []

    options = None
    if account_ids:
        options = AccountsBalanceGetRequestOptions(account_ids=list(account_ids))

    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        try:
            kwargs = {"access_token": token.reveal()}
            if options is not None:
                kwargs["options"] = options
            resp = api.accounts_balance_get(AccountsBalanceGetRequest(**kwargs)).to_dict()
            for raw in resp.get("accounts", []):
                accounts.append(shape_account(raw, health.institution_name))
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})

    return {"accounts": accounts, "warnings": warnings}


get_balances = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Balances"},
    name="get_balances",
)(_get_balances_impl)


_MAX_LOOKBACK_DAYS = 730  # ~2 years


def _clip_window(start_date: str, end_date: str) -> tuple[str, str, str | None]:
    """Return (start, end, warning_reason_or_None) clipped to the 2-year window."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    earliest = end - timedelta(days=_MAX_LOOKBACK_DAYS)
    if start < earliest:
        return earliest.isoformat(), end.isoformat(), (
            f"clipped start from {start.isoformat()} to {earliest.isoformat()} "
            "(Plaid max lookback ~2 years)"
        )
    return start.isoformat(), end.isoformat(), None


def _get_transactions_impl(
    start_date: str,
    end_date: str,
    account_ids: list[str] | None = None,
) -> dict:
    """Fetch transactions in [start_date, end_date] across all healthy Items.

    Dates are ISO YYYY-MM-DD. Uses Plaid /transactions/get with offset
    pagination (count=500 per page). If start_date is older than ~2 years
    before end_date, the window is clipped and a warning is emitted.
    """
    api = build_api()
    transactions: list[dict] = []
    warnings: list[dict] = []

    clipped_start, clipped_end, clip_reason = _clip_window(start_date, end_date)
    if clip_reason:
        warnings.append({"code": "WINDOW_CLIPPED", "reason": clip_reason, "message": clip_reason})

    base_options: dict = {"count": 500}
    if account_ids:
        base_options["account_ids"] = list(account_ids)

    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        offset = 0
        try:
            while True:
                options = TransactionsGetRequestOptions(**{**base_options, "offset": offset})
                resp = api.transactions_get(
                    TransactionsGetRequest(
                        access_token=token.reveal(),
                        start_date=date.fromisoformat(clipped_start),
                        end_date=date.fromisoformat(clipped_end),
                        options=options,
                    )
                ).to_dict()
                batch = resp.get("transactions", []) or []
                for raw in batch:
                    transactions.append(shape_transaction(raw))
                total = resp.get("total_transactions") or 0
                offset += len(batch)
                if offset >= total or not batch:
                    break
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})

    return {"transactions": transactions, "warnings": warnings}


get_transactions = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Transactions"},
    name="get_transactions",
)(_get_transactions_impl)


def _shape_stream(stream: dict, institution: str | None) -> dict:
    avg_amount_obj = stream.get("average_amount") or {}
    pfc = stream.get("personal_finance_category") or {}
    return {
        "stream_id": stream.get("stream_id"),
        "description": stream.get("description"),
        "merchant": stream.get("merchant_name"),
        "average_amount": avg_amount_obj.get("amount"),
        "frequency": stream.get("frequency"),
        "last_date": str(stream.get("last_date")) if stream.get("last_date") else None,
        "is_active": stream.get("is_active"),
        "category": pfc.get("primary"),
        "account_id": stream.get("account_id"),
        "institution": institution,
    }


def _get_recurring_transactions_impl() -> dict:
    """Return recurring inflow and outflow streams across all linked Items.

    Calls /accounts/get first per Item to collect account IDs (required by
    /transactions/recurring/get), then fetches recurring streams and shapes
    them into unified inflows/outflows lists.

    Returns:
        {"inflows": [...], "outflows": [...], "warnings": [...]}
    """
    api = build_api()
    inflows: list[dict] = []
    outflows: list[dict] = []
    warnings: list[dict] = []
    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        try:
            acct_resp = api.accounts_get(
                AccountsGetRequest(access_token=token.reveal())
            ).to_dict()
            account_ids = [a["account_id"] for a in acct_resp.get("accounts", [])]
            resp = api.transactions_recurring_get(
                TransactionsRecurringGetRequest(
                    access_token=token.reveal(),
                    account_ids=account_ids,
                )
            ).to_dict()
            for stream in resp.get("inflow_streams", []) or []:
                inflows.append(_shape_stream(stream, health.institution_name))
            for stream in resp.get("outflow_streams", []) or []:
                outflows.append(_shape_stream(stream, health.institution_name))
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})
    return {"inflows": inflows, "outflows": outflows, "warnings": warnings}


get_recurring_transactions = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Recurring Transactions"},
    name="get_recurring_transactions",
)(_get_recurring_transactions_impl)


def _get_liabilities_impl() -> dict:
    """Return credit, student-loan, and mortgage liability details across all linked Items.

    For Items where the liabilities product is not enabled, a per-Item warning
    with code PRODUCTS_NOT_SUPPORTED is emitted instead of failing the call.

    Returns:
        {"credit": [...], "student": [...], "mortgage": [...], "warnings": [...]}
    """
    api = build_api()
    credit: list[dict] = []
    student: list[dict] = []
    mortgage: list[dict] = []
    warnings: list[dict] = []
    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        try:
            resp = api.liabilities_get(
                LiabilitiesGetRequest(access_token=token.reveal())
            ).to_dict()
            liabs = resp.get("liabilities") or {}

            for c in liabs.get("credit", []) or []:
                aprs = c.get("aprs") or []
                # Prefer purchase_apr; fall back to first APR entry
                apr_entry = next(
                    (a for a in aprs if a.get("apr_type") == "purchase_apr"),
                    aprs[0] if aprs else {},
                )
                credit.append({
                    "account_id": c.get("account_id"),
                    "institution": health.institution_name,
                    "apr_percentage": apr_entry.get("apr_percentage"),
                    "last_payment_amount": c.get("last_payment_amount"),
                    "last_payment_date": str(c["last_payment_date"]) if c.get("last_payment_date") else None,
                    "last_statement_balance": c.get("last_statement_balance"),
                    "last_statement_issue_date": str(c["last_statement_issue_date"]) if c.get("last_statement_issue_date") else None,
                    "minimum_payment_amount": c.get("minimum_payment_amount"),
                    "next_payment_due_date": str(c["next_payment_due_date"]) if c.get("next_payment_due_date") else None,
                    "is_overdue": c.get("is_overdue"),
                })

            for s in liabs.get("student", []) or []:
                student.append({
                    "account_id": s.get("account_id"),
                    "institution": health.institution_name,
                    "interest_rate_percentage": s.get("interest_rate_percentage"),
                    "origination_principal_amount": s.get("origination_principal_amount"),
                    "outstanding_interest_amount": s.get("outstanding_interest_amount"),
                    "next_monthly_payment": s.get("next_monthly_payment"),
                    "last_payment_amount": s.get("last_payment_amount"),
                    "last_payment_date": str(s["last_payment_date"]) if s.get("last_payment_date") else None,
                    "minimum_payment_amount": s.get("minimum_payment_amount"),
                })

            for m in liabs.get("mortgage", []) or []:
                interest = m.get("interest_rate") or {}
                mortgage.append({
                    "account_id": m.get("account_id"),
                    "institution": health.institution_name,
                    "interest_rate_percentage": interest.get("percentage"),
                    "origination_principal_amount": m.get("origination_principal_amount"),
                    "next_monthly_payment": m.get("next_monthly_payment"),
                    "last_payment_amount": m.get("last_payment_amount"),
                    "last_payment_date": str(m["last_payment_date"]) if m.get("last_payment_date") else None,
                    "current_late_fee": m.get("current_late_fee"),
                    "escrow_balance": m.get("escrow_balance"),
                    "maturity_date": str(m["maturity_date"]) if m.get("maturity_date") else None,
                })

        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})
    return {"credit": credit, "student": student, "mortgage": mortgage, "warnings": warnings}


get_liabilities = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Liabilities"},
    name="get_liabilities",
)(_get_liabilities_impl)


def _get_investment_holdings_impl() -> dict:
    """Return investment holdings with security metadata across all linked Items.

    Joins holdings with the securities list returned in the same response to
    provide symbol, name, and security type. Adds institution to each holding.

    Returns:
        {"holdings": [...], "warnings": [...]}
    """
    api = build_api()
    holdings: list[dict] = []
    warnings: list[dict] = []
    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        try:
            resp = api.investments_holdings_get(
                InvestmentsHoldingsGetRequest(access_token=token.reveal())
            ).to_dict()
            secs_by_id = {s["security_id"]: s for s in resp.get("securities", []) or []}
            for h in resp.get("holdings", []) or []:
                shaped = shape_holding(h, secs_by_id)
                shaped["institution"] = health.institution_name
                holdings.append(shaped)
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})
    return {"holdings": holdings, "warnings": warnings}


get_investment_holdings = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Investment Holdings"},
    name="get_investment_holdings",
)(_get_investment_holdings_impl)


def _get_investment_transactions_impl(
    start_date: str,
    end_date: str,
) -> dict:
    """Fetch investment transactions in [start_date, end_date] across all healthy Items.

    Dates are ISO YYYY-MM-DD. Uses offset pagination (count=500 per page).
    If start_date is older than ~2 years before end_date, the window is clipped
    and a warning is emitted. Each transaction is joined with security metadata
    (ticker symbol, name) from the same response.

    Returns:
        {"investment_transactions": [...], "warnings": [...]}
    """
    api = build_api()
    investment_transactions: list[dict] = []
    warnings: list[dict] = []

    clipped_start, clipped_end, clip_reason = _clip_window(start_date, end_date)
    if clip_reason:
        warnings.append({"code": "WINDOW_CLIPPED", "reason": clip_reason, "message": clip_reason})

    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        offset = 0
        try:
            while True:
                resp = api.investments_transactions_get(
                    InvestmentsTransactionsGetRequest(
                        access_token=token.reveal(),
                        start_date=date.fromisoformat(clipped_start),
                        end_date=date.fromisoformat(clipped_end),
                        options=InvestmentsTransactionsGetRequestOptions(
                            count=500,
                            offset=offset,
                        ),
                    )
                ).to_dict()
                secs_by_id = {
                    s["security_id"]: s
                    for s in resp.get("securities", []) or []
                }
                batch = resp.get("investment_transactions", []) or []
                for t in batch:
                    investment_transactions.append({
                        "investment_transaction_id": t.get("investment_transaction_id"),
                        "account_id": t.get("account_id"),
                        "date": str(t.get("date")) if t.get("date") else None,
                        "type": t.get("type"),
                        "subtype": t.get("subtype"),
                        "amount": t.get("amount"),
                        "quantity": t.get("quantity"),
                        "price": t.get("price"),
                        "fees": t.get("fees"),
                        "currency": t.get("iso_currency_code"),
                        "symbol": secs_by_id.get(t.get("security_id"), {}).get("ticker_symbol"),
                        "name": secs_by_id.get(t.get("security_id"), {}).get("name"),
                        "institution": health.institution_name,
                    })
                total = resp.get("total_investment_transactions") or 0
                offset += len(batch)
                if offset >= total or not batch:
                    break
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})

    return {"investment_transactions": investment_transactions, "warnings": warnings}


get_investment_transactions = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Investment Transactions"},
    name="get_investment_transactions",
)(_get_investment_transactions_impl)


def _get_institutions_status_impl() -> dict:
    """Return health status for every linked Item/institution.

    No additional network calls beyond what ``all_items`` already makes (it
    uses the 5-minute health cache). Enumerates linked Items and reports their
    current health status.

    Returns:
        {"items": [{"env_key", "institution", "institution_id", "status", "reason"}, ...]}
    """
    api = build_api()
    items_out: list[dict] = []
    for env_key, token, health in all_items(api):
        items_out.append({
            "env_key": env_key,
            "institution": health.institution_name or env_key,
            "institution_id": health.institution_id,
            "status": health.status,
            "reason": health.reason,
        })
    return {"items": items_out}


get_institutions_status = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Get Institutions Status"},
    name="get_institutions_status",
)(_get_institutions_status_impl)


def _search_transactions_impl(
    query: str,
    start_date: str,
    end_date: str,
) -> dict:
    """Search transactions by keyword across merchant, name, and counterparty names.

    Fetches transactions in [start_date, end_date] and filters them with a
    case-insensitive substring match against:
    - ``merchant_name``
    - ``name``
    - ``counterparties[].name``

    The match is performed on the raw Plaid payload before shaping so that
    counterparty names (which are dropped by ``shape_transaction``) are
    searchable. Dates are ISO YYYY-MM-DD. The window is clipped to ~2 years
    and a WINDOW_CLIPPED warning is emitted when applicable.

    Returns:
        {"transactions": [...], "warnings": [...]}
    """
    api = build_api()
    transactions: list[dict] = []
    warnings: list[dict] = []

    clipped_start, clipped_end, clip_reason = _clip_window(start_date, end_date)
    if clip_reason:
        warnings.append({"code": "WINDOW_CLIPPED", "reason": clip_reason, "message": clip_reason})

    q = query.lower()

    for env_key, token, health in all_items(api):
        if health.status != "healthy":
            warnings.append(_warning_from_health(health))
            continue
        offset = 0
        try:
            while True:
                options = TransactionsGetRequestOptions(count=500, offset=offset)
                resp = api.transactions_get(
                    TransactionsGetRequest(
                        access_token=token.reveal(),
                        start_date=date.fromisoformat(clipped_start),
                        end_date=date.fromisoformat(clipped_end),
                        options=options,
                    )
                ).to_dict()
                batch = resp.get("transactions", []) or []
                for raw in batch:
                    merchant = (raw.get("merchant_name") or "").lower()
                    name = (raw.get("name") or "").lower()
                    counterparty_names = [
                        (cp.get("name") or "").lower()
                        for cp in (raw.get("counterparties") or [])
                    ]
                    if q in merchant or q in name or any(q in cp for cp in counterparty_names):
                        transactions.append(shape_transaction(raw))
                total = resp.get("total_transactions") or 0
                offset += len(batch)
                if offset >= total or not batch:
                    break
        except ApiException as e:
            mapped = map_plaid_error(e, health.institution_name)["error"]
            warnings.append({"institution": health.institution_name, **mapped})

    return {"transactions": transactions, "warnings": warnings}


search_transactions = mcp.tool(
    annotations={"readOnlyHint": True, "title": "Search Transactions"},
    name="search_transactions",
)(_search_transactions_impl)


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
    )
