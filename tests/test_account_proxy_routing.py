"""Offline checks for account-route snapshots, maintenance leases and diagnostics."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
HOME = tempfile.TemporaryDirectory(prefix="muse-proxy-routing-")
os.environ.update(MUSE2API_HOME=HOME.name, MUSE2API_PROFILE_ROOT=HOME.name,
                  MUSE2API_KEY="test-only")
import app
import requests

PROXY = {"server": "http://proxy.example:8080", "username": "fixture",
         "password": "secret-marker"}
acc = app.store.add_account({"hatch_sess": "cookie-marker", "hatch_vml": "fixture"})
aid = acc["id"]
acc["ok"] = True
patch_req = app.AccountPatch(proxy=app.AccountProxy(**PROXY))
public = app.patch_account(aid, patch_req)
assert public["proxy"]["has_password"] is True
assert "secret-marker" not in json.dumps(public)
assert acc["ok"] is None
assert acc["cookies"]["hatch_sess"] == "cookie-marker"

before_use = (acc["last_used"], acc["use_count"])
with app._account_operation(aid) as snapshot:
    assert app._account_proxy(snapshot) == PROXY
    assert snapshot is not acc
    try:
        app.patch_account(aid, app.AccountPatch(proxy=None))
    except app.HTTPException as exc:
        assert exc.status_code == 409
    else:
        raise AssertionError("busy proxy edit must be rejected")
    result = app._probe_account_sync(aid)
    assert result["ok"] is False and result["busy"] is True
    acc["proxy"] = {"server": "http://changed.example:80"}
    assert app._account_proxy(snapshot) == PROXY
acc["proxy"] = copy.deepcopy(PROXY)
assert aid not in app.ACCOUNT_POOL.busy_accounts
assert not hasattr(app.engine._ensure_local(), "proxy_snapshot")
assert (acc["last_used"], acc["use_count"]) == before_use

protected = {k: copy.deepcopy(acc.get(k)) for k in
             ("cookies", "cookies_exp", "ok", "expires_at", "last_keepalive", "synced_at")}
seen = []
def exit_request(session, url, **kwargs):
    assert session.trust_env is False
    assert url == "https://api.ipify.org?format=json"
    assert kwargs["allow_redirects"] is False
    assert "headers" not in kwargs and "cookies" not in kwargs
    assert kwargs["proxies"]["https"].startswith("http://fixture:")
    return SimpleNamespace(status_code=200, json=lambda: {"ip": "203.0.113.42"})

def renew(cookies, expires=None, **kwargs):
    seen.append(copy.deepcopy(kwargs))
    assert kwargs["proxy"] == PROXY and kwargs["wake_vm"] is False
    return {"ok": True, "status": "assigned"}
with patch("requests.Session.get", autospec=True, side_effect=exit_request), \
     patch.object(app.engine, "renew_session_http", side_effect=renew):
    result = app.test_account_proxy(aid)
assert result["ok"] and result["proxy_reachable"] and result["session_ok"]
assert result["exit_ip"] == "203.0.113.42"
assert "secret-marker" not in json.dumps(result)
assert all(acc.get(k) == value for k, value in protected.items())
assert seen

with patch("requests.Session.get", side_effect=requests.exceptions.ProxyError("secret-marker")), \
     patch.object(app.engine, "renew_session_http") as renew_mock:
    result = app.test_account_proxy(aid)
assert not result["ok"] and result["session_ok"] is None
assert "secret-marker" not in json.dumps(result)
renew_mock.assert_not_called()
assert all(acc.get(k) == value for k, value in protected.items())

for error in (app.MuseAuthError("fixture 401"),
              app.MuseGenerationError("fixture workspace unavailable"),
              app.MuseProxyError("fixture proxy authentication failed")):
    with patch("requests.Session.get", autospec=True, side_effect=exit_request), \
         patch.object(app.engine, "renew_session_http", side_effect=error):
        failed = app.test_account_proxy(aid)
    assert failed["ok"] is False
    assert all(acc.get(k) == value for k, value in protected.items())
    assert "secret-marker" not in json.dumps(failed)

status_before = copy.deepcopy(acc.get("proxy_status"))
app.patch_account(aid, app.AccountPatch(proxy=app.AccountProxy(**PROXY)))
assert acc.get("proxy_status") == status_before

# A cleanup error must never retain the account lease or proxy snapshot.
with patch.object(app.engine, "close_current_page", side_effect=RuntimeError("fixture cleanup")):
    try:
        with app._account_operation(aid):
            pass
    except RuntimeError:
        pass
assert aid not in app.ACCOUNT_POOL.busy_accounts
assert not hasattr(app.engine._ensure_local(), "proxy_snapshot")

# Quota and account tests use the selected account's exact proxy lease.
def quota(*args, **kwargs):
    assert aid in app.ACCOUNT_POOL.busy_accounts
    assert app._account_proxy(app.store.get_account(aid)) == PROXY
    assert kwargs["account_id"] == aid
    return {"found": True, "extra_left": "1B tokens left"}
with patch.object(app.engine, "start"), patch.object(app.engine, "quota", side_effect=quota):
    assert asyncio.run(app.query_quota(aid))["found"]
assert aid not in app.ACCOUNT_POOL.busy_accounts
assert not hasattr(app.engine._ensure_local(), "proxy_snapshot")

assert (acc["last_used"], acc["use_count"]) == before_use

# A proxy outage must not mark unknown authentication as a successful account.
acc["ok"] = None
with patch.object(app, "_renew_and_persist", side_effect=app.MuseProxyError("fixture proxy unavailable")), \
     patch.object(app.engine, "generate") as generate:
    try:
        app._run_generation_locked("fixture", "image", 10, account_id=aid,
                                   deadline=app.time.monotonic() + 10)
    except app.MuseProxyError:
        pass
    else:
        raise AssertionError("proxy failure was not propagated")
    generate.assert_not_called()
assert acc["ok"] is None
assert aid not in app.ACCOUNT_POOL.busy_accounts
assert not hasattr(app.engine._ensure_local(), "proxy_snapshot")

print("PASS: proxy snapshots, busy edits/maintenance, isolated diagnostics, no secret leak or false account success")
HOME.cleanup()
