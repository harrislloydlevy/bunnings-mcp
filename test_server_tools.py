#!/usr/bin/env python3
"""Offline tests that drive the real MCP server through the FastMCP client.

These call the tools over the in-memory transport — exactly the path an MCP
client uses — so they catch bugs that importing functions directly would miss.
In particular, ``@mcp.tool()`` rebinds the decorated name in
``bunnings_mcp.server``; a tool calling an imported client function of the same
name would capture the tool itself and return a coroutine, which then fails to
serialise here.

The client layer is patched, so no network and no credentials are used.

    python3 test_server_tools.py
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastmcp import Client

import bunnings_mcp.server as server

SENTINEL = {"ok": True}
DETAIL = {
    "wishlistId": "L1",
    "name": "L",
    "entries": [
        {"wishlistEntryId": "E1", "product": {"code": "S1"}, "quantity": "2"},
    ],
}
LIST_SLIM = {"wishLists": [{"wishlistId": "L1", "name": "L"}]}

CLIENT_RETURNS: dict[str, Any] = {
    "search_products": {"results": []},
    "get_product_price": {"value": 1.0},
    "get_product_location": [],
    "browse_category": {"results": []},
    "list_stores": {"pointOfServices": []},
    "get_cart": SENTINEL,
    "add_to_cart": SENTINEL,
    "update_cart_item": SENTINEL,
    "remove_from_cart": SENTINEL,
    "list_shopping_lists": LIST_SLIM,
    "create_shopping_list": DETAIL,
    "get_shopping_list": DETAIL,
    "add_to_shopping_list": DETAIL,
    "remove_from_shopping_list": SENTINEL,
    "delete_shopping_list": SENTINEL,
    "rename_shopping_list": DETAIL,
    "update_shopping_list_item": DETAIL,
    "move_shopping_list_items": SENTINEL,
    "set_active_shopping_list": SENTINEL,
    "add_shopping_list_to_cart": {"allAddedToCart": True},
}

# tool name -> (arguments, expected client function that must be called)
CASES: list[tuple[str, dict[str, Any], str]] = [
    ("search_products", {"query": "drill"}, "search_products"),
    ("get_product_price", {"sku": "1"}, "get_product_price"),
    ("get_product_location", {"sku": "1"}, "get_product_location"),
    ("browse_category", {"category": "tools"}, "browse_category"),
    ("list_stores", {}, "list_stores"),
    ("get_cart", {}, "get_cart"),
    ("add_to_cart", {"item_number": "1"}, "add_to_cart"),
    ("update_cart_item", {"entry_number": "1", "quantity": 2}, "update_cart_item"),
    ("remove_from_cart", {"entry_number": "1"}, "remove_from_cart"),
    ("list_shopping_lists", {}, "list_shopping_lists"),
    ("create_shopping_list", {"name": "n"}, "create_shopping_list"),
    ("get_shopping_list", {"list_id": "L1"}, "get_shopping_list"),
    ("add_to_shopping_list", {"list_id": "L1", "item_number": "S1"}, "add_to_shopping_list"),
    ("remove_from_shopping_list", {"list_id": "L1", "item_number": "S1"}, "remove_from_shopping_list"),
    ("delete_shopping_list", {"list_id": "L1"}, "delete_shopping_list"),
    ("rename_shopping_list", {"list_id": "L1", "name": "n2"}, "rename_shopping_list"),
    ("update_shopping_list_item", {"list_id": "L1", "quantity": 3, "item_number": "S1"}, "update_shopping_list_item"),
    ("move_shopping_list_items", {"from_list_id": "L1", "to_list_id": "L2", "item_numbers": ["S1"]}, "move_shopping_list_items"),
    ("set_active_shopping_list", {"list_id": "L1"}, "set_active_shopping_list"),
    ("add_shopping_list_to_cart", {"list_id": "L1"}, "add_shopping_list_to_cart"),
]


def patch_client() -> dict[str, Any]:
    """Replace every client function with a recorder; return the call log."""
    calls: dict[str, Any] = {}

    def make(name: str, ret: Any):
        def fn(*args: Any, **kwargs: Any) -> Any:
            calls[name] = (args, kwargs)
            return ret

        return fn

    for name, ret in CLIENT_RETURNS.items():
        setattr(server.client, name, make(name, ret))
    server.get_user_token = lambda cfg: "TOKEN"  # noqa: ARG005
    return calls


async def run_all() -> list[tuple[str, bool, str]]:
    calls = patch_client()
    results: list[tuple[str, bool, str]] = []

    async with Client(server.mcp) as client:
        for tool, args, expect_client in CASES:
            try:
                res = await client.call_tool(tool, args)
            except Exception as e:  # noqa: BLE001
                results.append((tool, False, f"call failed: {type(e).__name__}: {e}"))
                continue
            if res.is_error:
                results.append((tool, False, f"tool error: {res.data}"))
                continue
            if not isinstance(res.data, dict):
                results.append((tool, False, f"expected dict, got {type(res.data).__name__}"))
                continue
            if expect_client not in calls:
                results.append((tool, False, f"never called client.{expect_client}"))
                continue
            results.append((tool, True, ""))
    return results


def main() -> int:
    results = asyncio.run(run_all())
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")
    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} tools returned data via the MCP client")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
