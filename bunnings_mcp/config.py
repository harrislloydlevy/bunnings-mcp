"""Configuration for Bunnings MCP Server."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class CountryConfig:
    label: str
    base_url: str
    auth_base: str
    guest_client_id: str
    user_client_id: str
    retail_token_client_id: str
    okta_issuer: str
    okta_authorize_url: str
    okta_token_url: str
    redirect_uri: str
    country_code: str
    country_name: str
    locale: str
    currency: str
    default_store: str
    default_region: str


COUNTRIES: dict[str, CountryConfig] = {
    "au": CountryConfig(
        label="Bunnings AU",
        base_url="https://www.bunnings.com.au",
        auth_base="https://authorisation.api.bunnings.com.au",
        guest_client_id="budp_guest_user_au",
        user_client_id="0oa6rg2svokEfnLKF3l6",
        retail_token_client_id="budp_user_au_code",
        okta_issuer="https://login.bunnings.com.au",
        okta_authorize_url="https://login.bunnings.com.au/oauth2/v1/authorize",
        okta_token_url="https://login.bunnings.com.au/oauth2/v1/token",
        redirect_uri="https://www.bunnings.com.au/auth/redirect",
        country_code="AU",
        country_name="Australia",
        locale="en_AU",
        currency="AUD",
        default_store="6400",
        default_region="VICMetro",
    ),
    "nz": CountryConfig(
        label="Bunnings NZ",
        base_url="https://www.bunnings.co.nz",
        auth_base="https://authorisation.api.bunnings.co.nz",
        guest_client_id="budp_guest_user_nz",
        user_client_id="0oa6rg2svokEfnLKF3l6",
        retail_token_client_id="budp_user_nz_code",
        okta_issuer="https://login.bunnings.co.nz",
        okta_authorize_url="https://login.bunnings.co.nz/oauth2/v1/authorize",
        okta_token_url="https://login.bunnings.co.nz/oauth2/v1/token",
        redirect_uri="https://www.bunnings.co.nz/auth/redirect",
        country_code="NZ",
        country_name="New Zealand",
        locale="en_NZ",
        currency="NZD",
        default_store="9489",
        default_region="NI_Zone_9",
    ),
}

APIGEE_CLIENT_ID = "mHPVWnzuBkrW7rmt56XGwKkb5Gp9BJMk"
GUEST_SCOPE = "chk:exec cm:access ecom:access chk:pub vch:public bsk:pub"
USER_SCOPE = "openid profile email offline_access"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

TOKEN_CACHE_SECONDS = int(os.environ.get("BUNNINGS_GUEST_TOKEN_CACHE_SECONDS", "3600"))
TOKEN_REFRESH_SKEW = 300

REQUEST_TIMEOUT = 30
