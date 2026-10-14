"""Authentication module for Bunnings guest token and user login flows."""

from __future__ import annotations

import base64
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
from typing import Any

from .config import (
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


def _read_cached_user_token(cfg: CountryConfig) -> str | None:
    path = _user_token_cache_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if float(cached.get("cache_expires_at") or 0) <= time.time() + TOKEN_REFRESH_SKEW:
        return None
    return str(cached.get("token") or "") or None


def _write_cached_user_token(cfg: CountryConfig, token: str, expires_in: int) -> None:
    now = time.time()
    real_expires_at = now + max(0, expires_in)
    cache_expires_at = min(real_expires_at - TOKEN_REFRESH_SKEW, now + max(60, TOKEN_CACHE_SECONDS))
    cached = {
        "token": token,
        "real_expires_at": real_expires_at,
        "cache_expires_at": cache_expires_at,
        "country": cfg.country_code,
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


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_login_url(cfg: CountryConfig) -> dict[str, str]:
    """Generate an Okta authorization URL for user login.

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
    """Exchange an Okta authorization code for a Bunnings retail token."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": cfg.retail_token_client_id,
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "code_verifier": code_verifier,
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


def get_user_token(cfg: CountryConfig) -> str | None:
    """Get a valid cached user token, or None if not logged in."""
    return _read_cached_user_token(cfg)


def clear_user_token(cfg: CountryConfig) -> None:
    """Clear the cached user token."""
    _clear_cached_user_token(cfg)


def get_country_config(country: str) -> CountryConfig:
    """Get config for a country code, defaulting to NZ."""
    return COUNTRIES.get(country.lower(), COUNTRIES["nz"])
