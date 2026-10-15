#!/usr/bin/env python3
"""Offline regression tests for the MCP server tools.

Guards against a subtle bug: ``@mcp.tool()`` rebinds the decorated name in the
``bunnings_mcp.server`` module. A tool that calls an imported client function of
the *same* name therefore captures the tool itself and returns a coroutine
instead of the client result. The server avoids this by calling
``client.<fn>(...)``.

The client layer is patched, so this test uses no network and no credentials.

    python3 test_server_tools.py
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

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

    async def check(name: str, coro, expect_client: str | None = None) -> None:
        try:
            out = await coro
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"raised {type(e).__name__}: {e}"))
            return
        if inspect.iscoroutine(out):
            results.append((name, False, "returned a coroutine (name shadowing!)"))
            return
        if not isinstance(out, dict):
            results.append((name, False, f"expected dict, got {type(out).__name__}"))
            return
        if expect_client and expect_client not in calls:
            results.append((name, False, f"never called client.{expect_client}"))
            return
        results.append((name, True, ""))

    await check("search_products", server.search_products("drill"), "search_products")
    await check("get_product_price", server.get_product_price("1"), "get_product_price")
    await check("get_product_location", server.get_product_location("1"), "get_product_location")
    await check("browse_category", server.browse_category("tools"), "browse_category")
    await check("list_stores", server.list_stores(), "list_stores")
    await check("get_cart", server.get_cart(), "get_cart")
    await check("add_to_cart", server.add_to_cart("1"), "add_to_cart")
    await check("update_cart_item", server.update_cart_item("1", 2), "update_cart_item")
    await check("remove_from_cart", server.remove_from_cart("1"), "remove_from_cart")
    await check("list_shopping_lists", server.list_shopping_lists(), "list_shopping_lists")
    await check("create_shopping_list", server.create_shopping_list("n"), "create_shopping_list")
    await check("get_shopping_list", server.get_shopping_list("L1"), "get_shopping_list")
    await check("add_to_shopping_list", server.add_to_shopping_list("L1", "S1"), "add_to_shopping_list")
    await check(
        "remove_from_shopping_list",
        server.remove_from_shopping_list("L1", item_number="S1"),
        "remove_from_shopping_list",
    )
    await check("delete_shopping_list", server.delete_shopping_list("L1"), "delete_shopping_list")
    await check("rename_shopping_list", server.rename_shopping_list("L1", "n2"), "rename_shopping_list")
    await check(
        "update_shopping_list_item",
        server.update_shopping_list_item("L1", 3, item_number="S1"),
        "update_shopping_list_item",
    )
    await check(
        "move_shopping_list_items",
        server.move_shopping_list_items("L1", "L2", item_numbers=["S1"]),
        "move_shopping_list_items",
    )
    await check("set_active_shopping_list", server.set_active_shopping_list("L1"), "set_active_shopping_list")
    await check("add_shopping_list_to_cart", server.add_shopping_list_to_cart("L1"), "add_shopping_list_to_cart")
    return results


def main() -> int:
    results = asyncio.run(run_all())
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  -- ' + detail if detail else ''}")
    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"{len(results) - len(failed)}/{len(results)} tools returned data")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
