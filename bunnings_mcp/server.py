"""FastMCP server for Bunnings APIs."""

from __future__ import annotations

import re
from typing import Any

from fastmcp import FastMCP

from .auth import (
    clear_user_token,
    get_country_config,
    get_user_token,
    refresh_user_token,
    set_user_token,
)
from .client import (
    add_to_cart,
    add_to_shopping_list,
    browse_category,
    create_shopping_list,
    delete_shopping_list,
    get_cart,
    get_product_detail,
    get_product_fulfillment,
    get_product_location,
    get_product_price,
    get_shopping_list,
    list_shopping_lists,
    list_stores,
    remove_from_cart,
    remove_from_shopping_list,
    search_products,
    update_cart_item,
    BunningsAPIError,
)

mcp = FastMCP(
    "Bunnings",
    instructions="""
    Bunnings MCP Server - Access Bunnings product catalog, stores, pricing, and stock.
    
    Supports both Australia (AU) and New Zealand (NZ) stores.
    
    Product search, details, pricing, stock, and store locations are available
    without authentication using the guest token flow.
    
    Cart and shopping list management require an authenticated user token.
    To get a user token, log in to the Bunnings website and extract the
    bearer token from your session.
    """,
)


def _extract_products(payload: dict[str, Any], cfg, limit: int) -> list[dict[str, Any]]:
    """Extract and normalize products from a search payload."""
    products = []
    for item in payload.get("results") or []:
        raw = item.get("raw") if isinstance(item, dict) else None
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or raw.get("itemnumber") or raw.get("itemNumber") or "")
        if not code:
            continue
        store = cfg.default_store
        region = cfg.default_region.lower()
        price = raw.get("price")
        if price is None:
            price = raw.get(f"price_{store}")
        route = raw.get("productroutingurl") or raw.get("productRoutingUrl") or ""
        products.append({
            "sku": code,
            "name": raw.get("name") or raw.get("title") or "",
            "brand": raw.get("brandname") or "",
            "price": price,
            "currency": raw.get("currency") or cfg.currency,
            "unit": raw.get("unitofprice"),
            "rating": raw.get("rating"),
            "rating_count": raw.get("ratingcount"),
            "category": _category_label(raw),
            "image": raw.get("imageurl") or raw.get("thumbnailimageurl"),
            "source_url": cfg.base_url + route if route else None,
            "is_new": str(raw.get("newarrival", "")).lower() == "true",
            "is_best_seller": str(raw.get("bestseller", "")).lower() == "true",
        })
    return products[: max(1, limit)]


def _category_label(raw: dict[str, Any]) -> str:
    parts = []
    categories = raw.get("supercategories") or []
    if isinstance(categories, list):
        for value in categories:
            if isinstance(value, str) and "--" in value:
                parts.append(value.split("--", 1)[0])
    return " / ".join(parts)


def _parse_product_detail(data: dict[str, Any], cfg, sku: str) -> dict[str, Any]:
    """Parse the combined product detail response."""
    product = data.get("product") or {}
    price = data.get("price") or {}
    fulfilment = data.get("fulfilment") or {}
    aisle_data = data.get("aisle_locations") or []

    if not isinstance(product, dict) or not product.get("code"):
        return {"error": f"Could not find product detail data for {sku}"}

    brand = product.get("brand") or {}
    feature = product.get("feature") or {}
    in_store = fulfilment.get("inStorePickUpData") if isinstance(fulfilment, dict) else {}
    click_collect = fulfilment.get("clickNCollectData") if isinstance(fulfilment, dict) else {}
    delivery = fulfilment.get("deliveryData") if isinstance(fulfilment, dict) else {}

    aisle_locations = []
    if isinstance(aisle_data, list):
        for group in aisle_data:
            if not isinstance(group, dict):
                continue
            for loc in group.get("inStoreLocations") or []:
                if isinstance(loc, dict):
                    aisle_locations.append({
                        "aisle": loc.get("aisle"),
                        "bay": loc.get("bay"),
                        "sequence": loc.get("sequence"),
                    })

    categories = product.get("allCategories") or []
    cat_names = [c.get("displayName") for c in categories if isinstance(c, dict) and c.get("displayName")]

    return {
        "sku": str(product.get("code") or sku),
        "item_number": product.get("itemNumber"),
        "name": product.get("name"),
        "brand": brand.get("name") if isinstance(brand, dict) else None,
        "price": price.get("value") if isinstance(price, dict) else None,
        "price_display": price.get("formattedValue") if isinstance(price, dict) else None,
        "currency": price.get("currencyIso") if isinstance(price, dict) else cfg.currency,
        "rating": product.get("averageRating"),
        "rating_count": product.get("numberOfReviews"),
        "category": " / ".join(str(x) for x in cat_names),
        "key_selling_points": feature.get("pointers") if isinstance(feature, dict) else [],
        "description": feature.get("description") if isinstance(feature, dict) else None,
        "features": _feature_values(product),
        "fulfilment": {
            "in_store_available": in_store.get("inStorePickUpAvailable") if isinstance(in_store, dict) else None,
            "click_collect_available": click_collect.get("isClicknCollectAvailable") if isinstance(click_collect, dict) else None,
            "delivery_available": delivery.get("isDeliveryAvailable") if isinstance(delivery, dict) else None,
            "in_store_stock": in_store.get("stock") if isinstance(in_store, dict) else None,
            "in_store_text": in_store.get("inStoreStockTxt") if isinstance(in_store, dict) else None,
            "click_collect_stock": click_collect.get("stock") if isinstance(click_collect, dict) else None,
            "click_collect_text": click_collect.get("clickNCollectStockTxt") if isinstance(click_collect, dict) else None,
        },
        "aisle_locations": aisle_locations,
        "image": _primary_image(product),
    }


def _feature_values(product: dict[str, Any]) -> dict[str, str]:
    out = {}
    for group in product.get("classifications") or []:
        if not isinstance(group, dict):
            continue
        for feature in group.get("features") or []:
            if not isinstance(feature, dict):
                continue
            vals = feature.get("featureValues") or []
            value = vals[0].get("value") if vals and isinstance(vals[0], dict) else None
            name = feature.get("name")
            if name and value is not None:
                out[str(name)] = str(value)
    return out


def _primary_image(product: dict[str, Any]) -> str | None:
    images = product.get("images") or []
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and item.get("imageType") == "PRIMARY":
                return item.get("url") or item.get("thumbnailUrl")
        for item in images:
            if isinstance(item, dict) and (item.get("url") or item.get("thumbnailUrl")):
                return item.get("url") or item.get("thumbnailUrl")
    return None


def _parse_stores(payload: dict[str, Any], cfg) -> list[dict[str, Any]]:
    """Parse store list from API response."""
    all_stores = payload.get("pointOfServices") if isinstance(payload, dict) else []
    stores = []
    for raw in all_stores or []:
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        address = raw.get("address") or {}
        geo = raw.get("geoPoint") or {}
        stores.append({
            "code": raw.get("name"),
            "name": raw.get("displayName") or raw.get("description"),
            "region": raw.get("urlRegion") or "",
            "store_region": raw.get("storeRegion"),
            "pricing_region": raw.get("pricingRegion"),
            "address": address.get("formattedAddress"),
            "line1": address.get("line1"),
            "suburb": address.get("line2") or address.get("suburb"),
            "town": address.get("town"),
            "state": address.get("state"),
            "postal_code": address.get("postalCode"),
            "phone": address.get("phone"),
            "email": address.get("email"),
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
            "has_click_and_collect": bool(raw.get("hasClickAndCollect")),
            "has_delivery": bool(raw.get("hasDelivery")),
            "services": raw.get("storeServices") or [],
            "hours": _parse_hours(raw),
            "map_url": raw.get("mapUrl"),
        })
    return stores


def _parse_hours(store: dict[str, Any]) -> list[dict[str, Any]]:
    hours = ((store.get("openingHours") or {}).get("weekDayOpeningList") or [])
    out = []
    for item in hours:
        if not isinstance(item, dict):
            continue
        out.append({
            "day": item.get("weekDay"),
            "closed": bool(item.get("closed")),
            "open": ((item.get("openingTime") or {}).get("formattedHour")),
            "close": ((item.get("closingTime") or {}).get("formattedHour")),
        })
    return out


def _sku_from_value(value: str) -> str | None:
    """Extract SKU from a product URL or raw value."""
    match = re.search(r"_p([A-Za-z0-9]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9-]+", value):
        return value
    return None


# =============================================================================
# Product Tools
# =============================================================================


@mcp.tool()
async def search_products(
    query: str,
    country: str = "au",
    limit: int = 10,
) -> dict[str, Any]:
    """Search for products in the Bunnings catalog.

    Args:
        query: Search query (e.g. "cordless drill", "paint", "garden hose")
        country: Country code - "au" for Australia, "nz" for New Zealand
        limit: Maximum number of results to return (1-36)

    Returns:
        List of matching products with SKU, name, brand, price, and availability.
    """
    cfg = get_country_config(country)
    try:
        payload = search_products(cfg, query, limit=limit)
        products = _extract_products(payload, cfg, limit)
        return {
            "country": cfg.label,
            "query": query,
            "count": len(products),
            "total_count": payload.get("totalCount"),
            "products": products,
        }
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "query": query}


@mcp.tool()
async def get_product(
    sku: str,
    country: str = "au",
) -> dict[str, Any]:
    """Get detailed information about a specific product.

    Args:
        sku: Product SKU/code (e.g. "0123456") or full product URL
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Product details including name, brand, price, description, features,
        stock availability, and aisle/bay location.
    """
    cfg = get_country_config(country)
    sku = _sku_from_value(sku) or sku
    try:
        data = get_product_detail(cfg, sku)
        product = _parse_product_detail(data, cfg, sku)
        return {"country": cfg.label, "product": product}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "sku": sku}


@mcp.tool()
async def get_product_price(
    sku: str,
    country: str = "au",
) -> dict[str, Any]:
    """Get current price for a product.

    Args:
        sku: Product SKU/code
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Price information including value, formatted price, and currency.
    """
    cfg = get_country_config(country)
    sku = _sku_from_value(sku) or sku
    try:
        price = get_product_price(cfg, sku)
        return {"country": cfg.label, "sku": sku, "price": price}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "sku": sku}


@mcp.tool()
async def get_product_stock(
    sku: str,
    country: str = "au",
    location_code: str | None = None,
) -> dict[str, Any]:
    """Get stock/fulfillment availability for a product.

    Args:
        sku: Product SKU/code
        country: Country code - "au" for Australia, "nz" for New Zealand
        location_code: Optional store location code (defaults to country default store)

    Returns:
        Stock availability including in-store, click & collect, and delivery options.
    """
    cfg = get_country_config(country)
    sku = _sku_from_value(sku) or sku
    try:
        fulfilment = get_product_fulfillment(cfg, sku, location_code)
        return {"country": cfg.label, "sku": sku, "fulfilment": fulfilment}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "sku": sku}


@mcp.tool()
async def get_product_location(
    sku: str,
    country: str = "au",
    location_code: str | None = None,
) -> dict[str, Any]:
    """Get the aisle and bay location for a product in a store.

    Args:
        sku: Product SKU/code
        country: Country code - "au" for Australia, "nz" for New Zealand
        location_code: Optional store location code (defaults to country default store)

    Returns:
        Aisle and bay location information for in-store navigation.
    """
    cfg = get_country_config(country)
    sku = _sku_from_value(sku) or sku
    try:
        locations = get_product_location(cfg, sku, location_code)
        return {"country": cfg.label, "sku": sku, "locations": locations}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "sku": sku}


@mcp.tool()
async def browse_category(
    category: str,
    country: str = "au",
    limit: int = 10,
) -> dict[str, Any]:
    """Browse products by category path.

    Args:
        category: Category path (e.g. "tools/power-tools/drills", "paint-decorating")
        country: Country code - "au" for Australia, "nz" for New Zealand
        limit: Maximum number of results to return (1-36)

    Returns:
        List of products in the specified category.
    """
    cfg = get_country_config(country)
    try:
        payload = browse_category(cfg, category, limit=limit)
        products = _extract_products(payload, cfg, limit)
        return {
            "country": cfg.label,
            "category": category,
            "count": len(products),
            "total_count": payload.get("totalCount"),
            "products": products,
        }
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label, "category": category}


# =============================================================================
# Store Tools
# =============================================================================


@mcp.tool()
async def list_stores(
    country: str = "au",
    region: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """List Bunnings stores.

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand
        region: Optional region filter (e.g. "nsw", "vic", "uppernorthisland")
        limit: Maximum number of stores to return

    Returns:
        List of stores with name, address, phone, hours, and services.
    """
    cfg = get_country_config(country)
    try:
        payload = list_stores(cfg)
        stores = _parse_stores(payload, cfg)
        if region:
            needle = region.lower().replace(" ", "")
            stores = [
                s for s in stores
                if needle in " ".join(str(v or "") for v in s.values() if not isinstance(v, (list, dict))).lower().replace(" ", "")
            ]
        return {
            "country": cfg.label,
            "region": region,
            "count": len(stores[:limit]),
            "total_available": len(stores),
            "stores": stores[:limit],
        }
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


# =============================================================================
# Authentication Tools
# =============================================================================


@mcp.tool()
async def login(
    country: str = "au",
) -> dict[str, Any]:
    """Get instructions for logging in to Bunnings.

    Bunnings uses Okta with mandatory MFA, so the server cannot log in on the
    user's behalf. The user logs in via their normal browser, then provides the
    session cookies. With the refresh cookie included, the session renews
    automatically and indefinitely — one login lasts until the refresh token
    chain is revoked (weeks/months).

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Step-by-step login instructions.
    """
    cfg = get_country_config(country)
    return {
        "country": cfg.label,
        "instructions": (
            "1. Log in at https://www.bunnings.com.au (or bunnings.co.nz) in your browser.\n"
            "   Tip: use a private/incognito window so the session chain is only used here.\n"
            "2. Open DevTools (F12) → Application → Storage → Cookies → the bunnings domain.\n"
            "3. Copy the value of the 'UserAuthentication' cookie.\n"
            "4. Copy the value of the 'UserAuthenticationRefresh' cookie as well.\n"
            "5. Call complete_login with both values.\n"
            "   The refresh cookie enables automatic renewal — no repeated logins needed."
        ),
        "cookie_names": ["UserAuthentication", "UserAuthenticationRefresh"],
    }


@mcp.tool()
async def complete_login(
    user_token: str,
    refresh_token: str | None = None,
    country: str = "au",
) -> dict[str, Any]:
    """Complete login by providing the browser session cookies.

    Validates the UserAuthentication cookie via an authenticated Bunnings API
    call, caches it together with the UserAuthenticationRefresh cookie, and
    enables automatic renewal via the Bunnings IdentityServer.

    Args:
        user_token: Value of the 'UserAuthentication' cookie (a JWT, starts with 'eyJ')
        refresh_token: Value of the 'UserAuthenticationRefresh' cookie (looks like 64 hex chars + '-1')
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Login success status. After this, the session should persist automatically.
    """
    cfg = get_country_config(country)
    result = set_user_token(cfg, user_token, refresh_token=refresh_token)
    if result.get("logged_in"):
        warning = None
        if not refresh_token:
            warning = (
                "No refresh_token provided — the session will expire in ~10 minutes. "
                "Provide the UserAuthenticationRefresh cookie to enable automatic renewal."
            )
        return {**result, "warning": warning}
    return result


@mcp.tool()
async def logout(
    country: str = "au",
) -> dict[str, Any]:
    """Log out by clearing the cached user token.

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Logout confirmation.
    """
    cfg = get_country_config(country)
    clear_user_token(cfg)
    return {"country": cfg.label, "logged_out": True}


@mcp.tool()
async def check_login_status(
    country: str = "au",
) -> dict[str, Any]:
    """Check login status, including whether auto-renewal is enabled.

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand

    Returns:
        Login status, refresh capability, and token availability.
    """
    from .auth import get_user_refresh_token

    cfg = get_country_config(country)
    token = get_user_token(cfg)
    has_refresh = bool(get_user_refresh_token(cfg))
    return {
        "country": cfg.label,
        "logged_in": token is not None,
        "auto_renewal_enabled": has_refresh,
        "note": (
            "Session renews automatically via refresh token."
            if has_refresh
            else "No refresh token stored — session expires in ~10 minutes."
            if token
            else "Not logged in. Call login for instructions."
        ),
    }


# =============================================================================
# Cart Tools (require authenticated user token)
# =============================================================================


def _get_user_token_or_error(cfg, country: str) -> tuple[str | None, dict[str, Any] | None]:
    token = get_user_token(cfg)
    if token:
        return token, None
    return None, {
        "error": "Not logged in. Call the login tool first to authenticate.",
        "country": cfg.label,
    }


@mcp.tool()
async def get_cart(
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Get the current user's shopping cart.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Current cart contents with items, quantities, and totals.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        cart = get_cart(cfg, token)
        return {"country": cfg.label, "cart": cart}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def add_to_cart(
    item_number: str,
    quantity: int = 1,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Add an item to the shopping cart.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        item_number: Product item number/SKU to add
        quantity: Quantity to add (default 1)
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Updated cart contents.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        cart = add_to_cart(cfg, token, item_number, quantity)
        return {"country": cfg.label, "cart": cart}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def update_cart_item(
    entry_number: str,
    quantity: int,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Update the quantity of an item in the cart.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        entry_number: The cart entry number (see the 'entryNumber' field from get_cart)
        quantity: New quantity (0 to remove)
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Updated cart contents.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        cart = update_cart_item(cfg, token, entry_number, quantity)
        return {"country": cfg.label, "cart": cart}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def remove_from_cart(
    entry_number: str,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Remove an item from the shopping cart.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        entry_number: The cart entry number (see the 'entryNumber' field from get_cart)
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Updated cart contents.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        cart = remove_from_cart(cfg, token, entry_number)
        return {"country": cfg.label, "cart": cart}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


# =============================================================================
# Shopping List Tools (require authenticated user token)
# =============================================================================


@mcp.tool()
async def list_shopping_lists(
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """List the user's shopping/project lists.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        List of shopping/project lists.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        lists = list_shopping_lists(cfg, token)
        return {"country": cfg.label, "lists": lists}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def create_shopping_list(
    name: str,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Create a new shopping/project list.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        name: Name for the new list
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        The newly created list.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        new_list = create_shopping_list(cfg, token, name)
        return {"country": cfg.label, "list": new_list}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def get_shopping_list(
    list_id: str,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Get a specific shopping/project list with its items.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        list_id: The list ID
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        The shopping list with its items.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        shopping_list = get_shopping_list(cfg, token, list_id)
        return {"country": cfg.label, "list": shopping_list}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def add_to_shopping_list(
    list_id: str,
    item_number: str,
    quantity: int = 1,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Add an item to a shopping/project list.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        list_id: The list ID
        item_number: Product item number/SKU to add
        quantity: Quantity to add (default 1)
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Updated shopping list.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        updated = add_to_shopping_list(cfg, token, list_id, item_number, quantity)
        return {"country": cfg.label, "list": updated}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def remove_from_shopping_list(
    list_id: str,
    item_number: str,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Remove an item from a shopping/project list.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        list_id: The list ID
        item_number: Product item number/SKU to remove
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Updated shopping list.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        updated = remove_from_shopping_list(cfg, token, list_id, item_number)
        return {"country": cfg.label, "list": updated}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


@mcp.tool()
async def delete_shopping_list(
    list_id: str,
    country: str = "au",
    user_token: str | None = None,
) -> dict[str, Any]:
    """Delete a shopping/project list.

    Requires authentication. Use the login tool first, or provide a user_token.

    Args:
        list_id: The list ID to delete
        country: Country code - "au" for Australia, "nz" for New Zealand
        user_token: Optional bearer token (uses cached token if not provided)

    Returns:
        Deletion confirmation.
    """
    cfg = get_country_config(country)
    token = user_token or get_user_token(cfg)
    if not token:
        return {"error": "Not logged in. Call the login tool first to authenticate.", "country": cfg.label}
    try:
        result = delete_shopping_list(cfg, token, list_id)
        return {"country": cfg.label, "deleted": True, "result": result}
    except BunningsAPIError as e:
        return {"error": str(e), "country": cfg.label}


def main() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
