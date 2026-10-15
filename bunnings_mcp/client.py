"""HTTP client for Bunnings APIs."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from .auth import get_guest_token, clear_token, try_refresh_user_token
from .config import APIGEE_CLIENT_ID, DEFAULT_USER_AGENT, REQUEST_TIMEOUT, CountryConfig


class BunningsAPIError(Exception):
    """Raised when a Bunnings API call fails."""

    def __init__(self, message: str, status_code: int | None = None, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


def _build_url(cfg: CountryConfig, path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    path = path_or_url if path_or_url.startswith("/") else "/" + path_or_url
    return cfg.base_url + path


def _api_headers(
    cfg: CountryConfig,
    referer_path: str = "/",
    token: str | None = None,
    json_body: bool = False,
    user_token: str | None = None,
) -> dict[str, str]:
    token = token or get_guest_token(cfg)
    cookie = f"GuestAuthentication={token}"
    if user_token:
        cookie += f"; UserAuthentication={user_token}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-AU,en;q=0.9",
        "Authorization": "Bearer " + (user_token or token),
        "Cookie": cookie,
        "Referer": _build_url(cfg, referer_path),
        "User-Agent": DEFAULT_USER_AGENT,
        "clientId": APIGEE_CLIENT_ID,
        "correlationid": str(uuid.uuid1()),
        "country": cfg.country_code,
        "currency": cfg.currency,
        "locale": cfg.locale,
        "locationCode": cfg.default_store,
        "sessionid": str(uuid.uuid1()),
        "stream": "RETAIL",
        "userId": "anonymous",
        "X-region": cfg.default_region,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def api_request(
    cfg: CountryConfig,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    referer_path: str = "/",
    user_token: str | None = None,
    retry_auth: bool = True,
) -> Any:
    """Make an API request to Bunnings."""
    url = _build_url(cfg, path)
    token = get_guest_token(cfg)
    data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers=_api_headers(cfg, referer_path, token, body is not None, user_token),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        if e.code in (401, 403) and retry_auth and not user_token:
            clear_token(cfg)
            return api_request(cfg, path, method, body, referer_path, user_token, retry_auth=False)
        if e.code in (401, 403) and retry_auth and user_token:
            new_token = try_refresh_user_token(cfg, user_token)
            if new_token:
                return api_request(cfg, path, method, body, referer_path, new_token, retry_auth=False)
        raise BunningsAPIError(
            f"HTTP {e.code} from {url}: {raw[:500]}",
            status_code=e.code,
            response_body=raw,
        ) from e
    except urllib.error.URLError as e:
        raise BunningsAPIError(f"Network error calling {url}: {e.reason}") from e

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BunningsAPIError(f"Invalid JSON from {url}: {e}") from e

    status = payload.get("statusDetails") if isinstance(payload, dict) else None
    if isinstance(status, dict) and status.get("state") == "FAILURE":
        raise BunningsAPIError(
            f"{status.get('errorCode') or 'API'} from {url}: {status.get('description') or 'request failed'}"
        )
    if isinstance(payload, dict) and "data" in payload and isinstance(status, dict):
        return payload["data"]
    return payload


def search_products(
    cfg: CountryConfig,
    query: str,
    limit: int = 10,
    category_code: str | None = None,
    category_path: str | None = None,
) -> dict[str, Any]:
    """Search for products using the Coveo search API."""
    store = cfg.default_store
    region = cfg.default_region.lower()
    country = cfg.country_code
    clauses = [
        f"@availableinregions==({region})",
        f"@price_{store} > 0",
        "@isactive==true",
        f"@batchcountry==({country})",
        "@origin==(OPERATOR)",
    ]
    if category_code:
        clauses.insert(0, f"@supercategoriescode==({category_code})")

    visitor = str(uuid.uuid4())
    body: dict[str, Any] = {
        "debug": False,
        "enableDidYouMean": True,
        "enableDuplicateFiltering": False,
        "enableQuerySyntax": False,
        "facetOptions": {"freezeFacetOrder": True},
        "filterField": "@baseid",
        "filterFieldRange": 10,
        "lowerCaseOperators": True,
        "partialMatch": True,
        "partialMatchKeywords": 2,
        "partialMatchThreshold": "30%",
        "questionMark": True,
        "enableWordCompletion": True,
        "firstResult": 0,
        "isGuestUser": True,
        "numberOfResults": min(max(limit, 1), 36),
        "sortCriteria": "relevancy",
        "q": query,
        "aq": " AND ".join(clauses),
        "cq": f"@source==(PRODUCT_STREAM_{country})",
        "searchHub": "PRODUCT_LISTING" if category_code else "PRODUCT_SEARCH",
        "visitorId": visitor,
        "context": {
            "store": store,
            "country": cfg.country_name,
            "platform": "Web",
            "role": "retail",
            "website": country,
        },
        "context_website": country,
        "cu": cfg.currency,
        "de": "UTF-8",
        "analytics": {
            "clientId": visitor,
            "trackingId": country,
            "actionCause": "interfaceLoad" if category_code else "searchboxSubmit",
            "capture": True,
        },
        "extendedSearchOptions": {"includes": ["Banner", "SponsoredProducts", "Facets"]},
        "pipeline": "Variant_Product",
        "fieldsToInclude": _search_fields(cfg),
    }
    if category_path:
        body["tab"] = category_path.strip("/")

    referer = "/search/products?" + urllib.parse.urlencode({"q": query})
    return api_request(cfg, "/_apis/v1/coveo/search", method="POST", body=body, referer_path=referer)


def _search_fields(cfg: CountryConfig) -> list[str]:
    store = cfg.default_store
    region = cfg.default_region.lower()
    return [
        "source",
        "thumbnailimageurl",
        "supercategoriescode",
        "supercategoriesurl",
        "supercategories",
        "ratingcount",
        "brandiconurl",
        "title",
        "objecttype",
        "currency",
        "colorcount",
        f"price_{store}",
        "price",
        "rating",
        "stockstatus",
        "forhire",
        "orderingid",
        "bestseller",
        "productroutingurl",
        "brandcode",
        "categories",
        "brandname",
        "name",
        "itemnumber",
        "url",
        "newarrival",
        "imageurl",
        "availability",
        "code",
        "basicbundle",
        f"productRanges_{store}",
        f"productRanges_{region}",
        "productranges",
        "stockindicator",
        "familycolourname",
        "unitofprice",
        f"storeattributes_{store}",
        "isactive",
        "keysellingpoints",
        "agerestricted",
        "sellername",
        f"cprice_{store}",
        "comparisonunit",
        "comparisonunitofmeasure",
        "comparisonunitofmeasurecode",
        "promotionalcampaign",
        "promotionalcampaignstart",
        "promotionalcampaignend",
        "defaultofferid",
        "productoffers",
    ]


def get_product_detail(cfg: CountryConfig, sku: str) -> dict[str, Any]:
    """Get full product details by SKU."""
    referer = "/search/products?" + urllib.parse.urlencode({"q": sku})
    product_data = api_request(cfg, f"/_apis/v1/products/{urllib.parse.quote(sku)}?fields=FULL", referer_path=referer)
    price = api_request(cfg, f"/_apis/v2/products/{urllib.parse.quote(sku)}/priceInfo", referer_path=referer)
    fulfilment = api_request(
        cfg,
        f"/_apis/v2/products/{urllib.parse.quote(sku)}/fulfillment",
        method="POST",
        body={"includeVariantStock": False, "locationCode": cfg.default_store, "storeRadius": "200000"},
        referer_path=referer,
    )
    aisle = api_request(
        cfg,
        "/_apis/v1/item-api/locations?"
        + urllib.parse.urlencode({"locationCode": cfg.default_store, "productCode": sku}),
        referer_path=referer,
    )
    return {
        "product": product_data,
        "price": price,
        "fulfilment": fulfilment,
        "aisle_locations": aisle,
    }


def get_product_price(cfg: CountryConfig, sku: str) -> dict[str, Any]:
    """Get price information for a product."""
    referer = "/search/products?" + urllib.parse.urlencode({"q": sku})
    return api_request(cfg, f"/_apis/v2/products/{urllib.parse.quote(sku)}/priceInfo", referer_path=referer)


def get_product_fulfillment(cfg: CountryConfig, sku: str, location_code: str | None = None) -> dict[str, Any]:
    """Get fulfillment/stock information for a product."""
    referer = "/search/products?" + urllib.parse.urlencode({"q": sku})
    return api_request(
        cfg,
        f"/_apis/v2/products/{urllib.parse.quote(sku)}/fulfillment",
        method="POST",
        body={
            "includeVariantStock": False,
            "locationCode": location_code or cfg.default_store,
            "storeRadius": "200000",
        },
        referer_path=referer,
    )


def get_product_location(cfg: CountryConfig, sku: str, location_code: str | None = None) -> dict[str, Any]:
    """Get aisle/bay location for a product in a store."""
    return api_request(
        cfg,
        "/_apis/v1/item-api/locations?"
        + urllib.parse.urlencode({
            "locationCode": location_code or cfg.default_store,
            "productCode": sku,
        }),
    )


def list_stores(cfg: CountryConfig) -> dict[str, Any]:
    """List all stores for a country."""
    return api_request(cfg, f"/_apis/v1/stores/country/{cfg.country_code}?fields=FULL", referer_path="/stores")


def browse_category(cfg: CountryConfig, category: str, limit: int = 10) -> dict[str, Any]:
    """Browse products by category path."""
    path = category if category.startswith("/") else "/" + category
    if not path.startswith("/products/"):
        path = "/products/" + path.lstrip("/")
    category_code = path.rstrip("/").split("/")[-1]
    return search_products(cfg, "", limit=limit, category_code=category_code, category_path=path.replace("/products/", "", 1))


def _resolve_cart_id(
    cfg: CountryConfig, user_token: str, cart_id: str | None = None
) -> str | None:
    """Resolve the current cart ID.

    The cart ID is returned by ``POST /_apis/v1/carts`` (the ``data.code``
    field). ``/_apis/chkexp/baskets`` is NOT the cart and has no ID.
    """
    if cart_id:
        return str(cart_id)
    result = api_request(
        cfg,
        "/_apis/v1/carts?oldCartId=&fields=LITE&cartCreate=false",
        method="POST",
        user_token=user_token,
    )
    data = result.get("data", result) if isinstance(result, dict) else {}
    code = data.get("code") if isinstance(data, dict) else None
    return str(code) if code else None


def get_cart(cfg: CountryConfig, user_token: str, cart_id: str | None = None) -> dict[str, Any]:
    """Get the current user's cart. Requires authenticated user token."""
    if cart_id:
        return api_request(cfg, f"/_apis/v1/carts/{cart_id}?fields=LITE", user_token=user_token)
    # POST (not GET) creates/returns the cart; the ID is in data.code
    return api_request(
        cfg,
        "/_apis/v1/carts?oldCartId=&fields=LITE&cartCreate=false",
        method="POST",
        user_token=user_token,
    )


def add_to_cart(
    cfg: CountryConfig,
    user_token: str,
    item_number: str,
    quantity: int = 1,
    cart_id: str | None = None,
) -> dict[str, Any]:
    """Add an item to the cart. Requires authenticated user token."""
    cid = _resolve_cart_id(cfg, user_token, cart_id)
    if not cid:
        return {"error": "Could not determine cart ID", "country": cfg.label}
    path = (
        f"/_apis/v1/carts/{cid}/entries"
        f"?code={urllib.parse.quote(str(item_number))}&qty={int(quantity)}"
        f"&fields=LITE&validateStock=false&isMarketLink=false"
    )
    return api_request(cfg, path, method="POST", user_token=user_token)


def update_cart_item(
    cfg: CountryConfig,
    user_token: str,
    entry_number: str | int,
    quantity: int,
    cart_id: str | None = None,
) -> dict[str, Any]:
    """Update a cart entry's quantity by its entryNumber. Requires user token.

    Note: this takes the cart *entryNumber* (position in the cart), not the SKU.
    """
    cid = _resolve_cart_id(cfg, user_token, cart_id)
    if not cid:
        return {"error": "Could not determine cart ID", "country": cfg.label}
    return api_request(
        cfg,
        f"/_apis/v1/carts/{cid}/entries/{entry_number}",
        method="PATCH",
        body={"quantity": int(quantity)},
        user_token=user_token,
    )


def remove_from_cart(
    cfg: CountryConfig,
    user_token: str,
    entry_number: str | int,
    cart_id: str | None = None,
) -> dict[str, Any]:
    """Remove a cart entry by its entryNumber. Requires authenticated user token.

    Note: this takes the cart *entryNumber* (position in the cart), not the SKU.
    """
    cid = _resolve_cart_id(cfg, user_token, cart_id)
    if not cid:
        return {"error": "Could not determine cart ID", "country": cfg.label}
    return api_request(
        cfg,
        f"/_apis/v1/carts/{cid}/entries/{entry_number}",
        method="DELETE",
        user_token=user_token,
    )


def list_shopping_lists(cfg: CountryConfig, user_token: str) -> dict[str, Any]:
    """List user's shopping/project lists. Requires authenticated user token."""
    return api_request(
        cfg,
        "/_apis/v1/customer-management/users/wishlist?sortOrder=default&fields=SLIM",
        user_token=user_token,
    )


def create_shopping_list(cfg: CountryConfig, user_token: str, name: str) -> dict[str, Any]:
    """Create a new shopping/project list. Requires authenticated user token."""
    return api_request(
        cfg,
        "/_apis/v1/customer-management/users/wishlist",
        method="POST",
        body={"name": name},
        user_token=user_token,
    )


def get_shopping_list(cfg: CountryConfig, user_token: str, list_id: str) -> dict[str, Any]:
    """Get a specific shopping/project list. Requires authenticated user token."""
    return api_request(
        cfg,
        f"/_apis/v1/customer-management/users/wishlist/{list_id}",
        user_token=user_token,
    )


def add_to_shopping_list(
    cfg: CountryConfig, user_token: str, list_id: str, item_number: str, quantity: int = 1
) -> dict[str, Any]:
    """Add an item to a shopping/project list. Requires authenticated user token."""
    return api_request(
        cfg,
        "/_apis/v1/customer-management/users/wishlist",
        method="POST",
        body={
            "code": item_number,
            "list": "",
            "name": "My Project List",
            "productCategory": "",
            "productName": "",
            "productValue": 0,
            "quantity": str(quantity),
            "wishlistId": list_id,
            "wishlistName": "My Project List",
        },
        user_token=user_token,
    )


def remove_from_shopping_list(
    cfg: CountryConfig, user_token: str, list_id: str, item_number: str
) -> dict[str, Any]:
    """Remove an item from a shopping/project list. Requires authenticated user token."""
    return api_request(
        cfg,
        f"/_apis/v1/customer-management/users/wishlist/{list_id}/items/{item_number}",
        method="DELETE",
        user_token=user_token,
    )


def delete_shopping_list(cfg: CountryConfig, user_token: str, list_id: str) -> dict[str, Any]:
    """Delete a shopping/project list. Requires authenticated user token."""
    return api_request(
        cfg,
        f"/_apis/v1/customer-management/users/wishlist/{list_id}",
        method="DELETE",
        user_token=user_token,
    )
