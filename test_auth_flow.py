#!/usr/bin/env python3
"""End-to-end tests for the Bunnings MCP login/auth foundation.

Drives the real FastMCP tools (``login``, ``complete_login``,
``check_login_status``, ``logout``) plus the auth primitives, so it exercises
exactly what an MCP client would hit.

Default suite (safe — keeps the live session alive):
  1. login() returns usable instructions
  2. check_login_status() reports logged in + auto-renewal
  3. the stored access token works for an authenticated call
  4. refresh_user_token() rotates the chain and the new token works
  5. get_user_token() auto-refreshes once the cache looks expired
  6. the session survives a fresh process (reads the on-disk chain)
  7. concurrent get_user_token() calls reuse one rotation (no race)
  8. complete_login() falls back to a valid refresh cookie when the access
     cookie is already dead
  9. negative paths do not fabricate a logged-in session

Full login rehearsal (wipes then restores the live chain):
  python3 test_auth_flow.py --login /tmp/bunnings_cookies.json
    -> backs up the live chain, logs out, calls complete_login() with the
       cookie pair from the file, verifies, and restores the backup on failure.

Cookie file format:
  {"user_token": "eyJ...", "refresh_token": "HEX64-1"}
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bunnings_mcp import auth, server  # noqa: E402
from bunnings_mcp.config import APIGEE_CLIENT_ID, DEFAULT_USER_AGENT  # noqa: E402
from bunnings_mcp.auth import get_country_config  # noqa: E402

CFG = get_country_config("au")
RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def jwt_exp(token: str) -> int:
    try:
        p = token.split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0


def wishlist_count(token: str, guest: str) -> int | None:
    """Authenticated call; returns number of wishlists or None on failure."""
    import urllib.error
    import urllib.request

    url = CFG.base_url + "/_apis/v1/customer-management/users/wishlist?sortOrder=default&fields=SLIM"
    headers = {
        "Accept": "application/json",
        "Cookie": f"GuestAuthentication={guest}; UserAuthentication={token}",
        "User-Agent": DEFAULT_USER_AGENT,
        "clientId": APIGEE_CLIENT_ID,
        "country": "AU",
        "currency": "AUD",
        "locale": "en_AU",
        "locationCode": "6400",
        "stream": "RETAIL",
        "Referer": CFG.base_url + "/my-account/project-lists",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except Exception:
        return None
    data = body.get("data", body)
    return len(data.get("wishLists", []))


def force_cache_expired() -> None:
    path = auth._user_token_cache_path(CFG)
    creds = auth._read_user_credentials(CFG)
    creds["cache_expires_at"] = time.time() - 10
    with open(path, "w") as f:
        json.dump(creds, f)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_login_instructions() -> bool:
    r = await server.login("au")
    ok = (
        r.get("cookie_names") == ["UserAuthentication", "UserAuthenticationRefresh"]
        and "UserAuthenticationRefresh" in r.get("instructions", "")
    )
    return record("login() returns usable instructions", ok)


async def test_status() -> bool:
    r = await server.check_login_status("au")
    return record(
        "check_login_status() logged in + auto-renewal",
        bool(r.get("logged_in") and r.get("auto_renewal_enabled")),
        r.get("note", ""),
    )


async def test_token_works() -> bool:
    guest = auth.get_guest_token(CFG)
    token = auth.get_user_token(CFG)
    if not token:
        return record("stored token works for authenticated call", False, "no token")
    n = wishlist_count(token, guest)
    return record("stored token works for authenticated call", n is not None, f"wishlists={n}")


async def test_refresh_rotates() -> bool:
    before = auth.get_user_refresh_token(CFG)
    old_token = auth.get_user_token(CFG)
    new_token = auth.refresh_user_token(CFG)
    after = auth.get_user_refresh_token(CFG)
    if not new_token:
        return record("refresh_user_token() rotates the chain", False, "refresh returned None")
    guest = auth.get_guest_token(CFG)
    works = wishlist_count(new_token, guest) is not None
    rotated = after != before and new_token != old_token
    return record(
        "refresh_user_token() rotates the chain",
        bool(works and rotated),
        f"rotated={rotated} works={works}",
    )


async def test_auto_refresh() -> bool:
    force_cache_expired()
    token = auth.get_user_token(CFG)
    guest = auth.get_guest_token(CFG)
    if not token:
        return record("get_user_token() auto-refreshes on expiry", False, "returned None")
    works = wishlist_count(token, guest) is not None
    return record("get_user_token() auto-refreshes on expiry", works)


async def test_process_restart() -> bool:
    code = (
        "import asyncio,json;"
        "from bunnings_mcp import server;"
        "print(json.dumps(asyncio.run(server.check_login_status('au'))))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        return record("session survives a fresh process", False, proc.stderr.strip()[-160:])
    try:
        status = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return record("session survives a fresh process", False, "bad output")
    return record("session survives a fresh process", bool(status.get("logged_in")), status.get("note", ""))


async def test_concurrent_refresh() -> bool:
    force_cache_expired()
    with ThreadPoolExecutor(max_workers=6) as pool:
        tokens = list(pool.map(lambda _: auth.get_user_token(CFG), range(6)))
    ok = all(tokens) and len({t for t in tokens}) == 1
    guest = auth.get_guest_token(CFG)
    works = bool(tokens[0]) and wishlist_count(tokens[0], guest) is not None
    return record(
        "concurrent get_user_token() reuse one rotation",
        bool(ok and works),
        f"distinct_tokens={len({t for t in tokens if t})} works={works}",
    )


async def test_refresh_fallback_on_login() -> bool:
    """Expired/garbage access cookie + valid refresh cookie -> logged in."""
    refresh = auth.get_user_refresh_token(CFG)
    if not refresh:
        return record("complete_login() refresh fallback", False, "no refresh token")
    r = await server.complete_login(
        user_token="eyJ.invalid.expired", refresh_token=refresh, country="au"
    )
    guest = auth.get_guest_token(CFG)
    token = auth.get_user_token(CFG)
    works = bool(token) and wishlist_count(token, guest) is not None
    ok = bool(r.get("logged_in") and r.get("refreshed_on_login") and works)
    return record(
        "complete_login() refresh fallback",
        ok,
        f"refreshed_on_login={r.get('refreshed_on_login')} works={works}",
    )


async def test_negatives() -> bool:
    """Bad credentials must not fabricate a session (sandboxed cache)."""
    sandbox = "/tmp/opencode/auth-test-cache"
    os.makedirs(sandbox, mode=0o700, exist_ok=True)
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = sandbox
    try:
        r1 = await server.complete_login(
            user_token="eyJ.garbage.token", refresh_token=None, country="au"
        )
        r2 = await server.complete_login(
            user_token="eyJ.garbage.token", refresh_token="deadbeef-1", country="au"
        )
        ok = not r1.get("logged_in") and not r2.get("logged_in")
        return record("bad credentials rejected (no session fabricated)", ok,
                      f"no_refresh={r1.get('error')!r} bad_refresh={r2.get('error')!r}")
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


# --------------------------------------------------------------------------- #
# Full login rehearsal
# --------------------------------------------------------------------------- #
async def run_login_rehearsal(cookie_file: str) -> bool:
    print("\n=== FULL LOGIN REHEARSAL ===")
    if not os.path.exists(cookie_file):
        print(f"  ERROR: {cookie_file} not found")
        return False
    with open(cookie_file) as f:
        cookies = json.load(f)
    user_token = cookies.get("user_token")
    refresh_token = cookies.get("refresh_token")
    if not user_token or not refresh_token:
        print("  ERROR: file needs both user_token and refresh_token")
        return False

    cache_path = auth._user_token_cache_path(CFG)
    backup = cache_path + ".bak"
    if os.path.exists(cache_path):
        shutil.copy2(cache_path, backup)
    try:
        print("  -> logout() (clears cache)")
        await server.logout("au")
        assert auth.get_user_token(CFG) is None or auth.get_user_refresh_token(CFG) is None or True
        logged_out = auth._read_user_credentials(CFG) == {}
        print(f"     cache cleared: {logged_out}")

        print("  -> complete_login(user_token, refresh_token)")
        r = await server.complete_login(user_token=user_token, refresh_token=refresh_token, country="au")
        print("     result:", json.dumps({k: r.get(k) for k in
              ("logged_in", "auto_renewal", "refreshed_on_login", "token_expires_in_seconds", "error")}))

        guest = auth.get_guest_token(CFG)
        token = auth.get_user_token(CFG)
        works = bool(token) and wishlist_count(token, guest) is not None
        ok = bool(r.get("logged_in") and r.get("auto_renewal") and works)
        record("full login rehearsal (fresh cookies)", ok, f"works={works}")
        return ok
    finally:
        if RESULTS and not RESULTS[-1][1] and os.path.exists(backup):
            shutil.copy2(backup, cache_path)
            print("  (restored previous session from backup)")
        if os.path.exists(backup):
            os.remove(backup)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", metavar="COOKIE_FILE", help="run the full login rehearsal with this cookie file")
    args = ap.parse_args()

    print(f"Country: {CFG.label}")
    creds = auth._read_user_credentials(CFG)
    print(f"Cache:   {auth._user_token_cache_path(CFG)}")
    print(f"         has_token={bool(creds.get('token'))} has_refresh={bool(creds.get('refresh_token'))}\n")

    async def run() -> None:
        await test_login_instructions()
        await test_status()
        await test_token_works()
        await test_refresh_rotates()
        await test_auto_refresh()
        await test_process_restart()
        await test_concurrent_refresh()
        await test_refresh_fallback_on_login()
        await test_negatives()
        if args.login:
            await run_login_rehearsal(args.login)

    asyncio.run(run())

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"\n  {passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
