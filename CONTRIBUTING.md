# Contributing

Thanks for your interest. A few ground rules before you open a PR.

## Scope

`personal-finance-mcp` is deliberately narrow:

- **Read-only personal finance.** No write/mutation tools — no transfers, no account edits. The read-only posture is a security property, not a limitation.
- **Single-tenant.** One deployment per person. Multi-tenancy is out of scope.
- **Self-hosted only.** No hosted/SaaS option, no telemetry, no "phone home" code.
- **Plaid-backed.** Other aggregators (Finicit, Teller, etc.) are out of scope.

Non-trivial features — new tools, new deployment targets, anything that changes the security model — should open an issue first to discuss fit. Small fixes (typos, docs, clear bugs) can go straight to PR.

## Running tests

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

All tests use mocked Plaid calls — no network, no real credentials required.

## Style

- Match the surrounding code. The codebase is small; patterns are consistent within it.
- Don't bump pinned dependency versions without a reason. The pins in `requirements.txt` are known-good.
- Don't add optional dependencies for marginal features.
