"""Authentication module for Bunnings guest token and user login flows."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.cookiejar
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from .config import (
    APIGEE_CLIENT_ID,
    COUNTRIES,
    DEFAULT_USER_AGENT,
    GUEST_SCOPE,
    TOKEN_CACHE_SECONDS,
    TOKEN_REFRESH_SKEW,
    USER_SCOPE,
    CountryConfig,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _token_cache_path(cfg: CountryConfig) -> str:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache_home, "bunnings-mcp", f"guest-token-{cfg.country_code.lower()}.json")


def _read_cached_token(cfg: CountryConfig) -> str | None:
    path = _token_cache_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if float(cached.get("cache_expires_at") or 0) <= time.time() + TOKEN_REFRESH_SKEW:
        return None
    return str(cached.get("token") or "") or None


def _write_cached_token(cfg: CountryConfig, token: str, expires_in: int) -> None:
    now = time.time()
    real_expires_at = now + max(0, expires_in)
    cache_expires_at = min(real_expires_at - TOKEN_REFRESH_SKEW, now + max(60, TOKEN_CACHE_SECONDS))
    cached = {
        "token": token,
        "real_expires_at": real_expires_at,
        "cache_expires_at": cache_expires_at,
        "country": cfg.country_code,
    }
    path = _token_cache_path(cfg)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cached, f)
    except OSError:
        pass


def _clear_cached_token(cfg: CountryConfig) -> None:
    try:
        os.unlink(_token_cache_path(cfg))
    except OSError:
        pass


def _open_no_redirect(opener, url: str, headers: dict[str, str], timeout: int):
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        return opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e
        raise


def mint_guest_token(cfg: CountryConfig, timeout: int = 30) -> tuple[str, int]:
    """Mint a new guest token via the OAuth2 implicit flow."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)

    params = {
        "response_type": "token",
        "scope": GUEST_SCOPE,
        "client_id": cfg.guest_client_id,
        "redirect_uri": cfg.base_url + "/static/guest.html",
        "nonce": uuid.uuid4().hex[:18],
        "acr_values": "adtid:" + str(uuid.uuid1()),
    }
    current = cfg.auth_base + "/connect/authorize?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-NZ,en-AU;q=0.9,en;q=0.8",
        "Referer": cfg.base_url + "/",
        "User-Agent": DEFAULT_USER_AGENT,
    }

    last_body = ""
    location = ""
    try:
        for _ in range(12):
            resp = _open_no_redirect(opener, current, headers, timeout)
            location = resp.headers.get("Location", "")
            if not location:
                last_body = resp.read().decode("utf-8", "replace")[:300]
                break
            current = urllib.parse.urljoin(current, location)
            if urllib.parse.urlparse(current).fragment:
                location = current
                break
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Guest token bootstrap failed with HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Guest token bootstrap network error: {e.reason}") from e

    fragment = urllib.parse.parse_qs(urllib.parse.urlparse(location).fragment)
    token = (fragment.get("access_token") or fragment.get("id_token") or [""])[0]
    expires_in = int((fragment.get("expires_in") or ["0"])[0] or 0)
    if not token:
        raise RuntimeError("Guest token bootstrap did not return a token")
    return token, expires_in


def get_guest_token(cfg: CountryConfig, force_refresh: bool = False) -> str:
    """Get a valid guest token, minting a new one if needed."""
    if not force_refresh:
        cached = _read_cached_token(cfg)
        if cached:
            return cached
    token, expires_in = mint_guest_token(cfg)
    _write_cached_token(cfg, token, expires_in)
    return token


def clear_token(cfg: CountryConfig) -> None:
    """Clear the cached token for a country."""
    _clear_cached_token(cfg)


def _user_token_cache_path(cfg: CountryConfig) -> str:
    cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(cache_home, "bunnings-mcp", f"user-token-{cfg.country_code.lower()}.json")


def _read_user_credentials(cfg: CountryConfig) -> dict[str, Any]:
    """Read cached user credentials (access token + refresh token).

    Returns {"token": "...", "refresh_token": "...", "real_expires_at": float}.
    """
    path = _user_token_cache_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return cached


def _read_cached_user_token(cfg: CountryConfig) -> str | None:
    cached = _read_user_credentials(cfg)
    if float(cached.get("cache_expires_at") or 0) <= time.time() + TOKEN_REFRESH_SKEW:
        return None
    return str(cached.get("token") or "") or None


def _write_cached_user_token(
    cfg: CountryConfig,
    token: str,
    expires_in: int,
    refresh_token: str | None = None,
) -> None:
    now = time.time()
    real_expires_at = now + max(0, expires_in)
    cache_expires_at = min(real_expires_at - TOKEN_REFRESH_SKEW, now + max(60, TOKEN_CACHE_SECONDS))
    existing = _read_user_credentials(cfg)
    cached = {
        "token": token,
        "real_expires_at": real_expires_at,
        "cache_expires_at": cache_expires_at,
        "country": cfg.country_code,
        # refresh token chain — rotated on every use, so always persist the newest
        "refresh_token": refresh_token or existing.get("refresh_token") or "",
    }
    path = _user_token_cache_path(cfg)
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cached, f)
    except OSError:
        pass


def _clear_cached_user_token(cfg: CountryConfig) -> None:
    try:
        os.unlink(_user_token_cache_path(cfg))
    except OSError:
        pass


@contextlib.contextmanager
def _user_token_lock(cfg: CountryConfig) -> Iterator[None]:
    """Cross-process exclusive lock guarding refresh/write of the token chain.

    The refresh token is single-use: if two callers refresh concurrently with
    the same token, one rotates it and the other gets a 400, which would kill
    the session. Serialising refresh read-modify-write around a file lock makes
    concurrent ``get_user_token`` calls safe. Best-effort on platforms without
    ``fcntl`` (the lock is simply a no-op there).
    """
    path = _user_token_cache_path(cfg) + ".lock"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # Cache dir not writable — proceed without locking.
        yield
        return
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        os.close(fd)


def get_user_refresh_token(cfg: CountryConfig) -> str | None:
    """Get the cached refresh token (for auto-refresh), or None."""
    cached = _read_user_credentials(cfg)
    return str(cached.get("refresh_token") or "") or None


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_login_url(cfg: CountryConfig) -> dict[str, str]:
    """Generate an Okta authorization URL for user login.

    .. deprecated:: Abandoned.
       The callback cannot be completed server-side (requires browser Okta
       session context; returns ``loginCallbackError=disabled``). Kept for
       reference only — use the cookie-paste flow (``set_user_token``) instead.
       See BunningsAuth.md → "Failed Approaches".

    Returns a dict with the auth URL and the PKCE verifier (needed for token exchange).
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": cfg.user_client_id,
        "response_type": "code",
        "scope": USER_SCOPE,
        "redirect_uri": cfg.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    url = cfg.okta_authorize_url + "?" + urllib.parse.urlencode(params)
    return {
        "auth_url": url,
        "code_verifier": verifier,
        "state": state,
    }


def exchange_login_code(
    cfg: CountryConfig,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange an Okta authorization code for a Bunnings retail token.

    .. deprecated:: Abandoned.
       The Okta token endpoint returns ``401 invalid_client`` for this flow
       (it needs a server-side secret we do not have). Kept for reference only.
       See BunningsAuth.md → "Failed Approaches".
    """
    import base64 as _b64
    credentials = _b64.b64encode(f"{cfg.user_client_id}:".encode("ascii")).decode("ascii")
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": cfg.user_client_id,
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": code_verifier,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg.okta_token_url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
            "Authorization": f"Basic {credentials}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Token exchange failed with HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Token exchange network error: {e.reason}") from e

    token = payload.get("access_token") or ""
    if not token:
        raise RuntimeError(f"Token exchange did not return an access token: {payload}")

    expires_in = int(payload.get("expires_in") or 0)
    _write_cached_user_token(cfg, token, expires_in)
    return {
        "token": token,
        "expires_in": expires_in,
        "id_token": payload.get("id_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type", "Bearer"),
    }


def _refresh_token_grant(
    cfg: CountryConfig, refresh_token: str, timeout: int = 30
) -> dict[str, Any] | None:
    """Perform the OIDC refresh_token grant against the IdentityServer.

    Returns the raw token payload (access_token, refresh_token, expires_in, ...)
    or None if the grant was rejected / failed. Does NOT touch the cache.
    """
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": cfg.retail_token_client_id,  # e.g. "budp_user_au_code"
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = urllib.request.Request(
        cfg.auth_base + "/connect/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return None
    return payload


def _refresh_locked(cfg: CountryConfig, force: bool = False) -> str | None:
    """Refresh the access token under the cross-process lock.

    Re-checks the cache inside the lock (unless ``force``) so concurrent callers
    reuse one rotation. Persists the rotated pair immediately.
    """
    with _user_token_lock(cfg):
        if not force:
            cached = _read_cached_user_token(cfg)
            if cached:
                return cached
        refresh_token = get_user_refresh_token(cfg)
        if not refresh_token:
            return None
        payload = _refresh_token_grant(cfg, refresh_token)
        if not payload:
            return None
        new_token = str(payload["access_token"])
        expires_in = int(payload.get("expires_in") or 600)
        _write_cached_user_token(
            cfg,
            new_token,
            expires_in,
            refresh_token=str(payload.get("refresh_token") or refresh_token),
        )
        return new_token


def get_user_token(cfg: CountryConfig) -> str | None:
    """Get a valid user token, auto-refreshing via the refresh token if needed.

    Safe under concurrent calls: the refresh runs under a cross-process lock and
    re-checks the cache inside it, so parallel callers reuse a single rotation
    instead of racing to spend the same single-use refresh token.

    Returns None only if there is no session at all (not logged in), or the
    refresh chain has been revoked/expired.
    """
    cached_token = _read_cached_user_token(cfg)
    if cached_token:
        return cached_token
    # Cached access token expired — try to renew via the refresh token chain
    if get_user_refresh_token(cfg):
        refreshed = _refresh_locked(cfg)
        if refreshed:
            return refreshed
        # Refresh chain is dead — clear stale caches
        _clear_cached_user_token(cfg)
    return None


def _decode_jwt_expiry(token: str) -> int:
    """Extract the exp claim from a JWT without verifying signature."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return int(claims.get("exp") or 0)
    except Exception:
        return 0


def _validate_user_token(
    cfg: CountryConfig, user_token: str, guest_token: str
) -> tuple[str, dict[str, Any]]:
    """Validate an access token via an authenticated API call.

    Returns ``("ok"|"auth_error"|"other_error"|"network_error", payload)``.

    Validation uses the wishlist endpoint: ``/apis/auth/session`` returns 404
    server-side (Cloudflare fingerprinting), while the wishlist call works from
    urllib when GuestAuthentication + UserAuthentication are sent together.
    """
    url = cfg.base_url + "/_apis/v1/customer-management/users/wishlist?sortOrder=default&fields=SLIM"
    headers = {
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "Cookie": f"GuestAuthentication={guest_token}; UserAuthentication={user_token}",
        "Referer": cfg.base_url + "/my-account/project-lists",
        "User-Agent": DEFAULT_USER_AGENT,
        "clientId": APIGEE_CLIENT_ID,
        "correlationid": str(uuid.uuid1()),
        "country": cfg.country_code,
        "currency": cfg.currency,
        "locale": cfg.locale,
        "locationCode": cfg.default_store,
        "stream": "RETAIL",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return "ok", json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "auth_error", {}
        return "other_error", {}
    except (urllib.error.URLError, json.JSONDecodeError):
        return "network_error", {}


def _wishlist_count(payload: dict[str, Any]) -> int:
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    wishlists = data.get("wishLists", []) if isinstance(data, dict) else []
    return len(wishlists)


def _expires_in_from(token: str, fallback: int = 600) -> int:
    exp = _decode_jwt_expiry(token)
    return max(60, exp - int(time.time())) if exp else fallback


def set_user_token(
    cfg: CountryConfig,
    user_token: str,
    refresh_token: str | None = None,
) -> dict[str, Any]:
    """Set the user credentials by validating and caching them.

    The user provides the values of TWO cookies from their browser
    (after logging in at bunnings.com.au):
      - ``UserAuthentication``        → user_token (10-min access JWT)
      - ``UserAuthenticationRefresh`` → refresh_token (rotating, long-lived)

    If the access cookie has already expired but the refresh cookie is still
    valid, the session is renewed on the spot (via the refresh grant) instead of
    being rejected, so copying the cookies a few minutes late still works. The
    refresh token enables automatic, indefinite renewal (see
    ``refresh_user_token``); without it the session dies after ~10 minutes.
    """
    guest_token = get_guest_token(cfg)
    status, payload = _validate_user_token(cfg, user_token, guest_token)
    refreshed_on_login = False

    if status == "auth_error":
        # The access cookie is dead. If we were handed a refresh cookie, renew
        # now rather than bouncing the user.
        if not refresh_token:
            return {"error": "Invalid or expired token", "logged_in": False, "country": cfg.label}
        with _user_token_lock(cfg):
            grant = _refresh_token_grant(cfg, refresh_token)
            if not grant:
                return {
                    "error": "Access token expired and refresh token rejected",
                    "logged_in": False,
                    "country": cfg.label,
                }
            user_token = str(grant["access_token"])
            refresh_token = str(grant.get("refresh_token") or refresh_token)
            # Persist the rotated pair immediately — the refresh token is
            # single-use, so the replacement must never be lost.
            _write_cached_user_token(
                cfg, user_token, _expires_in_from(user_token), refresh_token=refresh_token
            )
            status, payload = _validate_user_token(cfg, user_token, guest_token)
            if status != "ok":
                return {
                    "error": "Refreshed token was rejected",
                    "logged_in": False,
                    "country": cfg.label,
                }
            refreshed_on_login = True
    elif status == "network_error":
        return {"error": "Could not validate token", "logged_in": False, "country": cfg.label}
    # "other_error" (e.g. 5xx): the token may still be valid; cache it and let
    # auto-refresh reconcile later.

    expires_in = _expires_in_from(user_token)
    with _user_token_lock(cfg):
        _write_cached_user_token(cfg, user_token, expires_in, refresh_token=refresh_token)

    return {
        "logged_in": True,
        "wishlists_visible": _wishlist_count(payload),
        "refresh_token_saved": bool(refresh_token),
        "auto_renewal": bool(refresh_token),
        "refreshed_on_login": refreshed_on_login,
        "token_expires_in_seconds": expires_in,
        "country": cfg.label,
    }


def refresh_user_token(cfg: CountryConfig) -> str | None:
    """Renew the user access token via the Bunnings IdentityServer.

    Uses the rotating refresh token (``UserAuthenticationRefresh`` cookie)
    with the public client ``budp_user_au_code`` at
    ``https://authorisation.api.bunnings.com.au/connect/token``,
    grant_type=refresh_token. No client secret required.

    The response contains a NEW access token AND a NEW refresh token —
    the previous refresh token is invalidated (single-use rotation), so the
    fresh one is persisted immediately.

    Returns the new access token, or None if the refresh chain is dead.
    """
    return _refresh_locked(cfg, force=True)


def try_refresh_user_token(cfg: CountryConfig, user_token: str | None = None) -> str | None:
    """Compatibility wrapper around refresh_user_token (ignores the old token)."""
    return refresh_user_token(cfg)


def clear_user_token(cfg: CountryConfig) -> None:
    """Clear the cached user token."""
    _clear_cached_user_token(cfg)


def get_country_config(country: str) -> CountryConfig:
    """Get config for a country code, defaulting to NZ."""
    return COUNTRIES.get(country.lower(), COUNTRIES["nz"])
