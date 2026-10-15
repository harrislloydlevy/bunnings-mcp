#!/usr/bin/env python3
"""Offline tests for stale / conflicting session handling.

The rotating refresh token is single-use: if the same login is *also* used by a
live browser session (or another tool), one side's rotation invalidates the
other and the chain goes dead. These tests verify that the server:

* classifies the token-endpoint failure precisely (``invalid_grant`` vs a
  transient network error),
* remembers a dead chain instead of silently discarding the session,
* returns a stable ``error_code`` plus an actionable ``hint`` to the customer,
* does NOT log the user out on a transient network blip.

Nothing here touches the network: the auth primitives are patched and the
credential cache lives in a throwaway temp directory.

    python3 test_session_conflict.py
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import urllib.error
from typing import Any
from unittest import mock

# Isolate the credential cache BEFORE importing the package.
_TMP = tempfile.mkdtemp(prefix="bunnings-session-test-")
os.environ["XDG_CACHE_HOME"] = _TMP

from fastmcp import Client  # noqa: E402

import bunnings_mcp.auth as auth  # noqa: E402
import bunnings_mcp.client as client  # noqa: E402
import bunnings_mcp.server as server  # noqa: E402
from bunnings_mcp.config import COUNTRIES  # noqa: E402

CFG = COUNTRIES["au"]

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  -- {detail}")


def _seed(access_token: str, refresh_token: str | None, *, expires_in: int = 600) -> None:
    auth._write_cached_user_token(CFG, access_token, expires_in, refresh_token=refresh_token)


def _fresh() -> None:
    auth._clear_cached_user_token(CFG)


def _http_error(code: int, body: str):
    def _raise(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(body.encode()))

    return _raise


# --------------------------------------------------------------------------- #
# 1. token-endpoint failure classification
# --------------------------------------------------------------------------- #
def test_grant_classification() -> None:
    print("\n=== TEST 1: refresh-grant failure classification ===")
    with mock.patch.object(auth.urllib.request, "urlopen", _http_error(400, '{"error":"invalid_grant"}')):
        result = auth._refresh_token_grant(CFG, "already-spent")
    check("invalid_grant -> 'invalid_grant'", result == (None, "invalid_grant"), repr(result))

    with mock.patch.object(auth.urllib.request, "urlopen", _http_error(503, "<html>boom</html>")):
        result = auth._refresh_token_grant(CFG, "x")
    check("unexpected status -> 'http_<code>'", result == (None, "http_503"), repr(result))

    def _network(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.URLError("connection refused")

    with mock.patch.object(auth.urllib.request, "urlopen", _network):
        result = auth._refresh_token_grant(CFG, "x")
    check("network failure -> 'network_error'", result == (None, "network_error"), repr(result))


# --------------------------------------------------------------------------- #
# 2. complete_login catches an already-spent refresh cookie
# --------------------------------------------------------------------------- #
def test_complete_login_stale_chain() -> None:
    print("\n=== TEST 2: set_user_token catches a spent refresh cookie ===")
    _fresh()
    with mock.patch.object(auth, "get_guest_token", lambda cfg, force_refresh=False: "guest"), \
            mock.patch.object(auth, "_validate_user_token", lambda cfg, tok, guest: ("auth_error", {})), \
            mock.patch.object(auth, "_refresh_token_grant", lambda cfg, rt, timeout=30: (None, "invalid_grant")):
        r = auth.set_user_token(CFG, "expired.jwt", "spent-refresh")
    check("not logged in", r.get("logged_in") is False, repr(r))
    check("error_code == refresh_token_rejected",
          r.get("error_code") == "refresh_token_rejected", repr(r))
    check("hint tells the user to use an isolated login",
          "isolated" in (r.get("hint") or "").lower(), repr(r.get("hint")))


# --------------------------------------------------------------------------- #
# 3. a dead chain is detected once and remembered
# --------------------------------------------------------------------------- #
def test_dead_chain_detection() -> None:
    print("\n=== TEST 3: dead chain is detected and remembered ===")
    _fresh()
    _seed("expired.jwt", "old-refresh", expires_in=0)

    calls = {"n": 0}

    def grant(cfg: Any, rt: Any, timeout: int = 30) -> Any:
        calls["n"] += 1
        return None, "invalid_grant"

    with mock.patch.object(auth, "_refresh_token_grant", grant):
        tok, reason = auth.user_session_status(CFG)
        check("no token returned", tok is None, repr(tok))
        check("reason is refresh_token_rejected",
              reason == "refresh_token_rejected", repr(reason))
        _, reason2 = auth.user_session_status(CFG)
        check("reason persists on the next call",
              reason2 == "refresh_token_rejected", repr(reason2))
        check("endpoint hit exactly once (not hammered)", calls["n"] == 1, f"calls={calls['n']}")

    info = auth.session_error_status(reason)
    check("session_error_status exposes the code",
          info.get("error_code") == "refresh_token_rejected", repr(info))
    check("session_error_status exposes a hint", bool(info.get("hint")), repr(info))

    # A fresh, valid login must clear the rejected flag.
    _seed("fresh.jwt", "new-refresh", expires_in=600)
    check("rejected flag cleared from the cache",
          auth._read_user_credentials(CFG).get("refresh_rejected") is None,
          repr(auth._read_user_credentials(CFG)))
    tok3, reason3 = auth.user_session_status(CFG)
    check("re-login clears the rejected flag",
          tok3 == "fresh.jwt" and reason3 is None, repr((tok3, reason3)))


# --------------------------------------------------------------------------- #
# 4. a transient failure must NOT log the user out
# --------------------------------------------------------------------------- #
def test_network_error_preserves_session() -> None:
    print("\n=== TEST 4: transient network error preserves the session ===")
    _fresh()
    _seed("expired.jwt", "keep-me", expires_in=0)
    with mock.patch.object(auth, "_refresh_token_grant",
                           lambda cfg, rt, timeout=30: (None, "network_error")):
        tok, reason = auth.user_session_status(CFG)
    check("no token right now", tok is None, repr(tok))
    check("reason is refresh_unreachable", reason == "refresh_unreachable", repr(reason))
    check("refresh token preserved", auth.get_user_refresh_token(CFG) == "keep-me",
          repr(auth.get_user_refresh_token(CFG)))
    check("not flagged as rejected",
          auth._read_user_credentials(CFG).get("refresh_rejected") is not True)


# --------------------------------------------------------------------------- #
# 5. a working refresh renews and rotates
# --------------------------------------------------------------------------- #
def test_working_refresh_renews() -> None:
    print("\n=== TEST 5: a working refresh renews and rotates ===")
    _fresh()
    _seed("expired.jwt", "r1", expires_in=0)
    payload = {"access_token": "new.jwt", "refresh_token": "r2", "expires_in": 600}
    with mock.patch.object(auth, "_refresh_token_grant",
                           lambda cfg, rt, timeout=30: (payload, None)):
        tok, reason = auth.user_session_status(CFG)
    check("renewed access token", tok == "new.jwt" and reason is None, repr((tok, reason)))
    check("rotated refresh token persisted", auth.get_user_refresh_token(CFG) == "r2",
          repr(auth.get_user_refresh_token(CFG)))
    check("not flagged as rejected",
          auth._read_user_credentials(CFG).get("refresh_rejected") is not True)


# --------------------------------------------------------------------------- #
# 5b. a short-lived (10 min) access token must stay cached, not rotate on every call
# --------------------------------------------------------------------------- #
def test_short_lived_token_stays_cached() -> None:
    print("\n=== TEST 5b: a 10-minute access token stays cached ===")
    _fresh()
    # Real Bunnings user access tokens live ~10 minutes (600s) and the refresh
    # skew is 300s, so the cache window must be ~300s — not zero.
    _seed("ten-min.jwt", "r1", expires_in=600)

    calls = {"n": 0}

    def grant(cfg: Any, rt: Any, timeout: int = 30) -> Any:
        calls["n"] += 1
        return {"access_token": "rotated.jwt", "refresh_token": "r2", "expires_in": 600}, None

    with mock.patch.object(auth, "_refresh_token_grant", grant):
        tok, reason = auth.user_session_status(CFG)
        check("short-lived token is reused from cache", tok == "ten-min.jwt" and reason is None,
              repr((tok, reason)))
        check("no needless refresh/rotation", calls["n"] == 0, f"refreshes={calls['n']}")
        # ...and once it really is stale it rotates exactly once.
        _seed("stale.jwt", "r1", expires_in=0)
        tok2, _ = auth.user_session_status(CFG)
        check("stale token rotates", tok2 == "rotated.jwt", repr(tok2))


# --------------------------------------------------------------------------- #
# 6. the client surfaces the session code on a 401 it cannot refresh
# --------------------------------------------------------------------------- #
def test_client_401_surfaces_code() -> None:
    print("\n=== TEST 6: authenticated 401 with a dead chain carries a code ===")

    def _raise(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorised", {}, io.BytesIO(b"{}"))

    err: Exception | None = None
    with mock.patch.object(client.urllib.request, "urlopen", _raise), \
            mock.patch.object(client, "get_guest_token", lambda cfg: "guest"), \
            mock.patch.object(client, "refresh_user_token_with_reason",
                              lambda cfg: (None, "invalid_grant")):
        try:
            client.api_request(CFG, "/_apis/v1/cart", user_token="stale")
        except client.BunningsAPIError as e:  # noqa: PERF203
            err = e
    check("raises BunningsAPIError", isinstance(err, client.BunningsAPIError), repr(err))
    check("carries error_code == refresh_token_rejected",
          getattr(err, "code", None) == "refresh_token_rejected", repr(getattr(err, "code", None)))


# --------------------------------------------------------------------------- #
# 7. the MCP tools expose the coded, actionable error
# --------------------------------------------------------------------------- #
async def test_tools_report_condition() -> None:
    print("\n=== TEST 7: MCP tools return a coded, actionable error ===")
    _fresh()
    # Simulate a session that logged in once (refresh stored) and whose chain
    # then died because it was also used elsewhere.
    _seed("expired.jwt", "old-refresh", expires_in=0)
    auth._mark_refresh_rejected(CFG)

    async with Client(server.mcp) as c:
        r = await c.call_tool("get_cart", {})
    data = r.data
    check("tool returned an object", isinstance(data, dict), repr(data))
    check("error_code is refresh_token_rejected",
          data.get("error_code") == "refresh_token_rejected", repr(data))
    check("hint present", bool(data.get("hint")), repr(data))

    async with Client(server.mcp) as c:
        r = await c.call_tool("check_login_status", {})
    st = r.data
    check("check_login_status: not logged in", st.get("logged_in") is False, repr(st))
    check("check_login_status: exposes the code",
          st.get("error_code") == "refresh_token_rejected", repr(st))
    check("check_login_status: disables auto-renewal claim",
          st.get("auto_renewal_enabled") is False, repr(st))

    # A never-logged-in user gets the plain not_logged_in code.
    _fresh()
    async with Client(server.mcp) as c:
        r = await c.call_tool("get_cart", {})
    check("never logged in -> not_logged_in",
          r.data.get("error_code") == "not_logged_in", repr(r.data))


# --------------------------------------------------------------------------- #
# 8. complete_login through the MCP client rejects a spent cookie
# --------------------------------------------------------------------------- #
async def test_complete_login_via_mcp() -> None:
    print("\n=== TEST 8: complete_login (via MCP) rejects a spent cookie ===")
    _fresh()
    with mock.patch.object(auth, "get_guest_token", lambda cfg, force_refresh=False: "guest"), \
            mock.patch.object(auth, "_validate_user_token", lambda cfg, tok, guest: ("auth_error", {})), \
            mock.patch.object(auth, "_refresh_token_grant", lambda cfg, rt, timeout=30: (None, "invalid_grant")):
        async with Client(server.mcp) as c:
            r = await c.call_tool(
                "complete_login",
                {"user_token": "expired", "refresh_token": "spent"},
            )
    check("error_code is refresh_token_rejected",
          r.data.get("error_code") == "refresh_token_rejected", repr(r.data))
    check("logged_in is False", r.data.get("logged_in") is False, repr(r.data))


# --------------------------------------------------------------------------- #
# 9. the login instructions stress an isolated login
# --------------------------------------------------------------------------- #
async def test_login_instructions() -> None:
    print("\n=== TEST 9: login instructions stress isolation ===")
    async with Client(server.mcp) as c:
        r = await c.call_tool("login", {})
    data = r.data
    blob = (data.get("important", "") + " " + data.get("instructions", "")).lower()
    check("mentions isolated login", "isolated" in blob, blob[:120])
    check("mentions a private/incognito window", "incognito" in blob, blob[:120])
    check("tells the user to close the window", "close" in blob, blob[:200])
    check("still names complete_login", "complete_login" in blob)


def main() -> int:
    try:
        test_grant_classification()
        test_complete_login_stale_chain()
        test_dead_chain_detection()
        test_network_error_preserves_session()
        test_working_refresh_renews()
        test_short_lived_token_stays_cached()
        test_client_401_surfaces_code()
        asyncio.run(test_tools_report_condition())
        asyncio.run(test_complete_login_via_mcp())
        asyncio.run(test_login_instructions())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
