import os
import subprocess
import sys

from fastapi.testclient import TestClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_horizon_guard_blocks_import():
    # Run a fresh subprocess with HORIZON set; importing link_helper must sys.exit.
    result = subprocess.run(
        [sys.executable, "-c", "import link_helper"],
        env={**os.environ, "HORIZON": "1"},
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0
    assert "must not run on Horizon" in (result.stderr + result.stdout)


def test_index_returns_html(fake_env_tokens):
    # Import inside the test so fake_env_tokens env is active before build_api() runs.
    # NOTE: link_helper.build_api() is called at import time, but we're using
    # fake env vars which produce a valid (if useless) PlaidApi instance.
    import importlib
    import link_helper
    importlib.reload(link_helper)
    client = TestClient(link_helper.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Plaid" in resp.text
    assert "Link a bank" in resp.text
