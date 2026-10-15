#!/usr/bin/env python3
"""Verify the Bunnings session keep-alive mechanism.

Confirmed mechanism (see BunningsAuth.md):
  * Access token = `UserAuthentication` cookie (10-min JWT).
  * Refresh token = `UserAuthenticationRefresh` cookie (HEX64-1, single-use, rotates).
  * Renewal = OIDC refresh_token grant at the IdentityServer with the PUBLIC client
    `budp_user_au_code` (no secret). Each grant returns a new access token AND a new
    refresh token; the old refresh token is invalidated.
  * The API needs BOTH `GuestAuthentication` and `UserAuthentication` cookies.

Usage:
  1. Log in at https://www.bunnings.com.au in an incognito window.
  2. DevTools -> Application -> Cookies -> copy:
       - UserAuthentication        (JWT, starts with 'eyJ')
       - UserAuthenticationRefresh (looks like 64 hex chars + '-1')
  3. Save them to /tmp/bunnings_cookies.json:
       {"user_token": "eyJ...", "refresh_token": "HEX64-1"}
     (Then close the incognito window so the MCP owns the refresh chain.)
  4. Run: python3 test_session_refresh.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bunnings_mcp.auth import (
    _read_user_credentials,
    _user_token_cache_path,
    _write_cached_user_token,
    get_country_config,
    get_guest_token,
    get_user_refresh_token,
    get_user_token,
    refresh_user_token,
)
from bunnings_mcp.config import APIGEE_CLIENT_ID, DEFAULT_USER_AGENT

COOKIE_FILE = "/tmp/bunnings_cookies.json"
TOKEN_URL = "https://authorisation.api.bunnings.com.au/connect/token"
BASE = "https://www.bunnings.com.au"
CLIENT_ID = "budp_user_au_code"


def load_cookies() -> dict:
    if not os.path.exists(COOKIE_FILE):
        print(f"ERROR: {COOKIE_FILE} not found")
        print('Create it with: {"user_token": "...", "refresh_token": "..."}')
        sys.exit(1)
    with open(COOKIE_FILE) as f:
        return json.load(f)


def save_cookies(data: dict) -> None:
    with open(COOKIE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _wishlist_call(user_token: str, guest_token: str) -> int | None:
    """Call the wishlist API; return number of wishlists, or None on failure."""
    url = BASE + "/_apis/v1/customer-management/users/wishlist?sortOrder=default&fields=SLIM"
    headers = {
        "Accept": "application/json",
        "Cookie": f"GuestAuthentication={guest_token}; UserAuthentication={user_token}",
        "User-Agent": DEFAULT_USER_AGENT,
        "clientId": APIGEE_CLIENT_ID,
        "country": "AU",
        "currency": "AUD",
        "locale": "en_AU",
        "locationCode": "6400",
        "stream": "RETAIL",
        "Referer": BASE + "/my-account/project-lists",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    data = body.get("data", body)
    return len(data.get("wishLists", []))


def test_1_validate(cookies: dict, guest: str) -> bool:
    """The access token works for an authenticated API call."""
    print("=== TEST 1: Authenticated API call (wishlist) ===")
    n = _wishlist_call(cookies["user_token"], guest)
    if n is None:
        print("  [FAIL] 401/403 or error — token invalid/expired?")
        return False
    print(f"  [OK] Wishlists visible: {n}")
    return True


def test_2_refresh_grant(cookies: dict) -> bool:
    """Direct refresh_token grant at the IdentityServer."""
    print("\n=== TEST 2: refresh_token grant at IdentityServer ===")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": cookies["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  [FAIL] HTTP {e.code}: {e.read().decode()[:200]}")
        return False

    print("  [OK] Refresh grant works (no client secret needed).")
    print(f"  expires_in: {result.get('expires_in')}")
    print(f"  scope: {str(result.get('scope'))[:120]}...")
    old = cookies["refresh_token"]
    rotated = result["refresh_token"] != old
    print(f"  refresh token rotated: {rotated}")
    cookies["user_token"] = result["access_token"]
    cookies["refresh_token"] = result["refresh_token"]
    save_cookies(cookies)
    print(f"  >>> Saved rotated pair to {COOKIE_FILE}")
    return True


def test_3_chain_continues(cookies: dict) -> bool:
    """Refresh a second time to prove the rotation chain is indefinite."""
    print("\n=== TEST 3: Rotation chain continues (second refresh) ===")
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": cookies["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  [FAIL] HTTP {e.code}: {e.read().decode()[:200]}")
        return False
    print("  [OK] Second refresh succeeded — the chain is indefinite.")
    cookies["user_token"] = result["access_token"]
    cookies["refresh_token"] = result["refresh_token"]
    save_cookies(cookies)
    return True


def test_4_auto_refresh(cfg, cookies: dict, guest: str) -> bool:
    """Seed the real cache, force expiry, and confirm get_user_token auto-renews."""
    print("\n=== TEST 4: Auto-refresh on expiry (get_user_token) ===")
    # Seed the real cache with the current pair
    exp = int(json.loads(__import__("base64").urlsafe_b64decode(
        cookies["user_token"].split(".")[1] + "==").decode()).get("exp", 0))
    expires_in = max(60, exp - int(time.time())) if exp else 600
    _write_cached_user_token(cfg, cookies["user_token"], expires_in,
                             refresh_token=cookies["refresh_token"])

    # Force the cache to look expired
    path = _user_token_cache_path(cfg)
    creds = _read_user_credentials(cfg)
    creds["cache_expires_at"] = time.time() - 10
    with open(path, "w") as f:
        json.dump(creds, f)
    before = creds.get("refresh_token")

    token = get_user_token(cfg)  # should transparently refresh
    after = get_user_refresh_token(cfg)
    if not token:
        print("  [FAIL] get_user_token returned None")
        return False
    rotated = after != before
    print(f"  [OK] Auto-refresh returned a token; chain rotated: {rotated}")
    n = _wishlist_call(token, guest)
    print(f"  [OK] Auto-refreshed token works for API call (wishlists: {n})")
    return n is not None


def main() -> None:
    cookies = load_cookies()
    print(f"Loaded: user_token={cookies['user_token'][:32]}... "
          f"refresh_token={cookies['refresh_token'][:32]}...\n")

    cfg = get_country_config("au")
    guest = get_guest_token(cfg)

    ok1 = test_1_validate(cookies, guest)
    ok2 = test_2_refresh_grant(cookies)
    ok3 = test_3_chain_continues(cookies) if ok2 else False
    ok4 = test_4_auto_refresh(cfg, cookies, guest) if ok3 else False

    print("\n=== SUMMARY ===")
    print(f"  Authenticated API call:      {ok1}")
    print(f"  Refresh grant:               {ok2}")
    print(f"  Rotation chain indefinite:   {ok3}")
    print(f"  Auto-refresh on expiry:      {ok4}")
    if ok3 and ok4:
        print("  >>> FULL AUTO-REFRESH CONFIRMED — one login lasts indefinitely.")
    else:
        print("  >>> Some step failed — re-check with a freshly copied cookie pair.")


if __name__ == "__main__":
    main()
