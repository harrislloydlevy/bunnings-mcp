#!/usr/bin/env python3
"""Comprehensive live test suite for the Bunnings MCP server.

Drives the server through the FastMCP client (the same path an MCP consumer
uses) and exercises **every** tool, including optional parameters. Where a tool
mutates state, the change is verified with a follow-up MCP read call
(get_shopping_list / list_shopping_lists / get_cart / check_login_status).

All mutations are scoped to throwaway data:
  * temp project lists are created and deleted,
  * cart entries added for the test are removed,
  * the active list is restored,
  * the user session file is backed up and restored around the logout test.

Run:  python3 test_mcp_suite.py
Exit: 0 if every case passes, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from fastmcp import Client

import bunnings_mcp.server as server
from bunnings_mcp.auth import (
    _user_token_cache_path,  # noqa: PLC2701  (need the cache path for backup)
    get_country_config,
    get_user_token,
)


class SkipTest(Exception):
    """Raised to skip a case whose precondition is missing."""


def _cache_path() -> str:
    return _user_token_cache_path(get_country_config("au"))


def _read_cache_bytes() -> bytes | None:
    try:
        with open(_cache_path(), "rb") as f:
            return f.read()
    except OSError:
        return None


def _write_cache_bytes(data: bytes) -> None:
    path = _cache_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _cart_entries(cart: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for group in (cart.get("deliveryOrderGroups") or []) + (cart.get("pickupOrderGroups") or []):
        entries.extend(group.get("entries") or [])
    return entries


def _cart_map(cart: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["product"]["code"]: e for e in _cart_entries(cart)}


def _list_codes(detail: dict[str, Any]) -> set[str]:
    return {e["product"]["code"] for e in (detail.get("entries") or [])}


def _entry_by_code(detail: dict[str, Any], code: str) -> dict[str, Any] | None:
    for e in detail.get("entries") or []:
        if e["product"]["code"] == code:
            return e
    return None


class Suite:
    def __init__(self, client: Client):
        self.client = client
        self.results: list[tuple[str, str, str]] = []
        self.ctx: dict[str, Any] = {}
        self.created_lists: list[str] = []
        self.created_cart_entries: list[str] = []
        self.token_backup: bytes | None = None
        self.initial_cart_codes: set[str] = set()
        self.initial_list_ids: set[str] = set()
        self.initial_active_ids: set[str] = set()

    # ---- MCP helpers -----------------------------------------------------

    async def raw(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        res = await self.client.call_tool(tool, args or {})
        if res.is_error:
            raise AssertionError(f"{tool}: transport error: {res.data}")
        if not isinstance(res.data, dict):
            raise AssertionError(f"{tool}: expected object result, got {type(res.data).__name__}")
        return res.data

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call a tool, raising if the tool reports an error."""
        data = await self.raw(tool, args)
        if data.get("error"):
            raise AssertionError(f"{tool}: {data['error']}")
        return data

    def require(self, cond: Any, msg: str) -> None:
        if not cond:
            raise AssertionError(msg)

    async def run_case(self, name: str, coro) -> None:
        try:
            await coro
            self.results.append((name, "PASS", ""))
        except SkipTest as e:
            self.results.append((name, "SKIP", str(e)))
        except AssertionError as e:
            self.results.append((name, "FAIL", str(e)))
        except Exception as e:  # noqa: BLE001
            self.results.append((name, "FAIL", f"{type(e).__name__}: {e}"))

    # ---- read-back helpers ----------------------------------------------

    async def get_list(self, list_id: str) -> dict[str, Any]:
        return (await self.call("get_shopping_list", {"list_id": list_id}))["list"]

    async def get_cart(self) -> dict[str, Any]:
        return (await self.call("get_cart", {}))["cart"]

    async def list_ids(self) -> dict[str, str]:
        data = await self.call("list_shopping_lists", {})
        return {w["wishlistId"]: w.get("name") for w in data["lists"]["wishLists"]}

    # ---- setup / teardown -----------------------------------------------

    async def setup(self) -> None:
        cart = await self.get_cart()
        self.initial_cart_codes = set(_cart_map(cart))
        self.ctx["cart_code"] = cart.get("code", "")
        lists = await self.call("list_shopping_lists", {})
        wl = lists["lists"]["wishLists"]
        self.initial_list_ids = {w["wishlistId"] for w in wl}
        self.initial_active_ids = {w["wishlistId"] for w in wl if w.get("isActive")}
        if self.initial_list_ids:
            self.ctx["real_list"] = sorted(self.initial_list_ids)[0]

        # Candidate SKUs not currently in the cart (so tests never disturb it).
        chosen: list[str] = []
        for query in ("drill", "paint", "garden hose", "hammer", "saw"):
            if len(chosen) >= 5:
                break
            r = await self.call("search_products", {"query": query, "limit": 10})
            for p in r.get("products") or []:
                sku = p["sku"]
                if sku not in self.initial_cart_codes and sku not in chosen:
                    chosen.append(sku)
                if len(chosen) >= 5:
                    break
        if len(chosen) < 4:
            raise RuntimeError(f"could not find enough test SKUs (got {chosen})")
        self.ctx["sku_product"] = chosen[0]
        self.ctx["product_url"] = f"https://www.bunnings.com.au/x_p{chosen[0]}"
        self.ctx["sku_cart"] = chosen[1]
        self.ctx["sku_l1"] = chosen[2]
        self.ctx["sku_l2"] = chosen[3]
        self.ctx["sku_l3"] = chosen[4] if len(chosen) > 4 else chosen[0]

    async def cleanup(self) -> None:
        # Restore active list if we changed it and it still exists.
        try:
            lists = await self.list_ids()
            want_active = [i for i in self.initial_active_ids if i in lists]
            changed = self.ctx.get("active_changed_to")
            if want_active and changed and changed != want_active[0]:
                await self.call("set_active_shopping_list", {"list_id": want_active[0]})
        except Exception:  # noqa: BLE001
            pass

        # Remove cart entries we created.
        for entry_number in self.created_cart_entries:
            try:
                await self.call("remove_from_cart", {"entry_number": entry_number})
            except Exception:  # noqa: BLE001
                try:
                    server.client.remove_from_cart(get_country_config("au"), get_user_token(get_country_config("au")), entry_number)
                except Exception:  # noqa: BLE001
                    pass

        # Delete temp lists.
        for list_id in self.created_lists:
            try:
                await self.call("delete_shopping_list", {"list_id": list_id})
            except Exception:  # noqa: BLE001
                try:
                    server.client.delete_shopping_list(get_country_config("au"), get_user_token(get_country_config("au")), list_id)
                except Exception:  # noqa: BLE001
                    pass

        # Restore the session file if a backup exists and the file is gone.
        if self.token_backup is not None and _read_cache_bytes() is None:
            _write_cache_bytes(self.token_backup)

    # ---- verification of end state --------------------------------------

    async def verify_restored(self) -> list[tuple[str, str, str]]:
        checks: list[tuple[str, str, str]] = []
        cart = await self.get_cart()
        codes = set(_cart_map(cart))
        checks.append((
            "cart restored",
            "PASS" if codes == self.initial_cart_codes else "FAIL",
            f"{sorted(codes)} vs {sorted(self.initial_cart_codes)}",
        ))
        ids = set((await self.list_ids()).keys())
        checks.append((
            "lists restored",
            "PASS" if ids == self.initial_list_ids else "FAIL",
            f"{sorted(ids)} vs {sorted(self.initial_list_ids)}",
        ))
        checks.append((
            "session alive",
            "PASS" if _read_cache_bytes() is not None else "FAIL",
            "user token cache present",
        ))
        return checks

    # =====================================================================
    # Cases
    # =====================================================================

    async def case_login(self) -> None:
        r = await self.call("login", {"country": "au"})
        self.require("complete_login" in r["instructions"], "login instructions missing complete_login")
        self.require(r["cookie_names"] == ["UserAuthentication", "UserAuthenticationRefresh"], "cookie names wrong")
        self.require(r["country"] == "Bunnings AU", "au label wrong")
        rnz = await self.call("login", {"country": "nz"})
        self.require(rnz["country"] == "Bunnings NZ", "nz label wrong")

    async def case_check_login_status(self) -> None:
        r = await self.call("check_login_status", {})
        self.require(r["logged_in"] is True, "should be logged in")
        self.require(r["auto_renewal_enabled"] is True, "auto renewal should be enabled")
        self.require(r["country"] == "Bunnings AU", "country label wrong")

    async def case_complete_login(self) -> None:
        creds = json.loads(_read_cache_bytes() or b"{}")
        self.require(creds.get("token") and creds.get("refresh_token"), "no cached credentials to reuse")
        r = await self.call(
            "complete_login",
            {"user_token": creds["token"], "refresh_token": creds["refresh_token"], "country": "au"},
        )
        self.require(r.get("logged_in") is True, f"complete_login failed: {r}")
        self.require(r.get("refresh_token_saved") is True, "refresh token not saved")
        st = await self.call("check_login_status", {})
        self.require(st["logged_in"] is True, "status not logged in after complete_login")

    async def case_logout_and_restore(self) -> None:
        self.token_backup = _read_cache_bytes()
        self.require(self.token_backup is not None, "no session file to back up")

        r = await self.call("logout", {"country": "au"})
        self.require(r.get("logged_out") is True, "logout did not report success")
        st = await self.call("check_login_status", {})
        self.require(st["logged_in"] is False, "still logged in after logout")

        _write_cache_bytes(self.token_backup)  # restore the session
        st = await self.call("check_login_status", {})
        self.require(st["logged_in"] is True, "session not restored")
        lists = await self.call("list_shopping_lists", {})
        self.require("lists" in lists, "authenticated read failed after restore")

    async def case_search_products(self) -> None:
        r = await self.call("search_products", {"query": "cordless drill"})
        self.require(r["count"] > 0, "no results")
        self.require(all(p["sku"] for p in r["products"]), "a product is missing a sku")
        self.require(r["count"] <= 10, "default limit not applied")

    async def case_search_options(self) -> None:
        r = await self.call("search_products", {"query": "drill", "limit": 1})
        self.require(r["count"] == 1, f"limit not honoured: {r['count']}")
        rnz = await self.call("search_products", {"query": "drill", "country": "nz", "limit": 2})
        self.require(rnz["country"] == "Bunnings NZ", "nz country not honoured")
        self.require(rnz["count"] >= 1, "no nz results")

    async def case_get_product(self) -> None:
        sku = self.ctx["sku_product"]
        r = await self.call("get_product", {"sku": sku})
        p = r["product"]
        self.require(p.get("sku") == sku, "sku mismatch")
        self.require(p.get("name"), "no product name")
        # URL form (exercises SKU extraction)
        r2 = await self.call("get_product", {"sku": self.ctx["product_url"]})
        self.require(r2["product"].get("sku") == sku, "URL->sku resolution failed")

    async def case_get_product_price(self) -> None:
        r = await self.call("get_product_price", {"sku": self.ctx["sku_product"]})
        price = r["price"]
        self.require(isinstance(price, dict) and ("value" in price or "formattedValue" in price), f"no price: {price}")

    async def case_get_product_stock(self) -> None:
        sku = self.ctx["sku_product"]
        r = await self.call("get_product_stock", {"sku": sku})
        self.require(isinstance(r["fulfilment"], dict) and r["fulfilment"], "no fulfilment data")
        r2 = await self.call("get_product_stock", {"sku": sku, "location_code": "7374"})
        self.require(isinstance(r2["fulfilment"], dict) and r2["fulfilment"], "no fulfilment at override store")

    async def case_get_product_location(self) -> None:
        sku = self.ctx["sku_product"]
        r = await self.call("get_product_location", {"sku": sku})
        self.require("locations" in r, "no locations key")
        r2 = await self.call("get_product_location", {"sku": sku, "location_code": "7374"})
        self.require("locations" in r2, "no locations key at override store")

    async def case_browse_category(self) -> None:
        r = await self.call("browse_category", {"category": "paint-decorating", "limit": 3})
        self.require(r["count"] >= 1, "no products in category")

    async def case_list_stores(self) -> None:
        r = await self.call("list_stores", {})
        self.require(r["count"] > 0, "no stores")
        r2 = await self.call("list_stores", {"limit": 3})
        self.require(len(r2["stores"]) <= 3, "limit not applied")
        r3 = await self.call("list_stores", {"region": "nsw", "limit": 5})
        self.require(r3["count"] >= 1, "no nsw stores")

    async def case_get_cart(self) -> None:
        r = await self.call("get_cart", {})
        cart = r["cart"]
        self.require(cart.get("code"), "cart has no code")
        self.require(isinstance(_cart_entries(cart), list), "cart entries malformed")

    async def case_get_cart_user_token(self) -> None:
        token = get_user_token(get_country_config("au"))
        self.require(token, "no user token")
        r = await self.call("get_cart", {"user_token": token})
        self.require(r["cart"].get("code") == self.ctx["cart_code"], "explicit token returned wrong cart")

    async def case_add_update_remove_cart(self) -> None:
        sku = self.ctx["sku_cart"]
        r = await self.call("add_to_cart", {"item_number": sku, "quantity": 2})
        self.require("cart" in r, "add_to_cart returned no cart")
        entry = _cart_map(await self.get_cart()).get(sku)
        self.require(entry is not None, "added sku not in cart")
        entry_number = str(entry["entryNumber"])
        self.created_cart_entries.append(entry_number)
        self.require(int(entry["quantity"]) == 2, f"expected qty 2, got {entry['quantity']}")

        await self.call("update_cart_item", {"entry_number": entry_number, "quantity": 4})
        entry = _cart_map(await self.get_cart()).get(sku)
        self.require(entry and int(entry["quantity"]) == 4, f"qty update failed: {entry}")

        await self.call("remove_from_cart", {"entry_number": entry_number})
        self.created_cart_entries.remove(entry_number)
        self.require(sku not in _cart_map(await self.get_cart()), "sku still in cart after removal")

    async def case_create_list(self) -> None:
        name = "ZZ MCP Suite A"
        r = await self.call("create_shopping_list", {"name": name})
        list_id = r["list"]["wishlistId"]
        self.created_lists.append(list_id)
        self.ctx["list_a"] = list_id

        ids = await self.list_ids()
        self.require(ids.get(list_id) == name, "new list not visible in list_shopping_lists")
        detail = await self.get_list(list_id)
        self.require(detail["name"] == name, "detail name mismatch")
        self.require((detail.get("entries") or []) == [], "new list should be empty")

    async def case_add_items(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        await self.call("add_to_shopping_list", {"list_id": list_id, "item_number": self.ctx["sku_l1"], "quantity": 1})
        await self.call("add_to_shopping_list", {"list_id": list_id, "item_number": self.ctx["sku_l2"], "quantity": 2})
        detail = await self.get_list(list_id)
        qtys = {e["product"]["code"]: e["quantity"] for e in detail["entries"]}
        self.require(qtys.get(self.ctx["sku_l1"]) == "1", f"l1 qty wrong: {qtys}")
        self.require(qtys.get(self.ctx["sku_l2"]) == "2", f"l2 qty wrong: {qtys}")

    async def case_duplicate_add_rejected(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        before = await self.get_list(list_id)
        # The backend rejects a duplicate add outright (BUS-4008) instead of
        # silently ignoring it, so the tool surfaces an error and nothing changes.
        r = await self.raw(
            "add_to_shopping_list",
            {"list_id": list_id, "item_number": self.ctx["sku_l2"], "quantity": 5},
        )
        self.require(
            r.get("error") and "already exists" in r["error"].lower(),
            f"duplicate add should be rejected: {r}",
        )
        after = await self.get_list(list_id)
        self.require(len(after["entries"]) == len(before["entries"]), "duplicate add created a new entry")
        e = _entry_by_code(after, self.ctx["sku_l2"])
        self.require(e and e["quantity"] == "2", f"duplicate add changed quantity: {e}")

    async def case_update_qty_by_item_number(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        await self.call("update_shopping_list_item", {"list_id": list_id, "quantity": 7, "item_number": self.ctx["sku_l1"]})
        e = _entry_by_code(await self.get_list(list_id), self.ctx["sku_l1"])
        self.require(e and e["quantity"] == "7", f"qty not updated: {e}")

    async def case_update_qty_by_entry_id(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        e2 = _entry_by_code(await self.get_list(list_id), self.ctx["sku_l2"])
        self.require(e2, "l2 entry missing")
        await self.call(
            "update_shopping_list_item",
            {"list_id": list_id, "quantity": 3, "wishlist_entry_id": e2["wishlistEntryId"]},
        )
        e = _entry_by_code(await self.get_list(list_id), self.ctx["sku_l2"])
        self.require(e and e["quantity"] == "3", f"qty not updated by entry id: {e}")

    async def case_rename_list(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        new = "ZZ MCP Suite A renamed"
        await self.call("rename_shopping_list", {"list_id": list_id, "name": new})
        self.require((await self.get_list(list_id))["name"] == new, "rename not reflected in detail")
        self.require((await self.list_ids()).get(list_id) == new, "rename not reflected in list_shopping_lists")

    async def case_create_dest_and_move(self) -> None:
        list_a = self.ctx.get("list_a")
        self.require(list_a, "list A missing")
        r = await self.call("create_shopping_list", {"name": "ZZ MCP Suite B"})
        list_b = r["list"]["wishlistId"]
        self.created_lists.append(list_b)
        self.ctx["list_b"] = list_b

        await self.call(
            "move_shopping_list_items",
            {"from_list_id": list_a, "to_list_id": list_b, "item_numbers": [self.ctx["sku_l2"]]},
        )
        a = await self.get_list(list_a)
        b = await self.get_list(list_b)
        self.require(self.ctx["sku_l2"] not in _list_codes(a), "sku_l2 still in source")
        self.require(self.ctx["sku_l2"] in _list_codes(b), "sku_l2 not in destination")
        self.require(self.ctx["sku_l1"] in _list_codes(a), "sku_l1 wrongly moved")

    async def case_move_back_by_entry_id(self) -> None:
        list_a = self.ctx.get("list_a")
        list_b = self.ctx.get("list_b")
        self.require(list_a and list_b, "lists missing")
        e = _entry_by_code(await self.get_list(list_b), self.ctx["sku_l2"])
        self.require(e, "sku_l2 missing from destination")
        await self.call(
            "move_shopping_list_items",
            {"from_list_id": list_b, "to_list_id": list_a, "wishlist_entry_ids": [e["wishlistEntryId"]]},
        )
        self.require(self.ctx["sku_l2"] in _list_codes(await self.get_list(list_a)), "not moved back to A")
        self.require(self.ctx["sku_l2"] not in _list_codes(await self.get_list(list_b)), "still in B")

    async def case_set_active(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        await self.call("set_active_shopping_list", {"list_id": list_id})
        lists = (await self.call("list_shopping_lists", {}))["lists"]["wishLists"]
        active = {w["wishlistId"] for w in lists if w.get("isActive")}
        self.require(list_id in active, f"list A not active: {active}")
        self.ctx["active_changed_to"] = list_id

    async def case_remove_item_by_item_number(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        await self.call("remove_from_shopping_list", {"list_id": list_id, "item_number": self.ctx["sku_l1"]})
        self.require(self.ctx["sku_l1"] not in _list_codes(await self.get_list(list_id)), "sku_l1 not removed")

    async def case_remove_item_by_entry_id(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        await self.call("add_to_shopping_list", {"list_id": list_id, "item_number": self.ctx["sku_l3"], "quantity": 1})
        e = _entry_by_code(await self.get_list(list_id), self.ctx["sku_l3"])
        self.require(e, "sku_l3 not added")
        result = await self.call("remove_from_shopping_list", {"list_id": list_id, "wishlist_entry_id": e["wishlistEntryId"]})
        self.require(result.get("removed"), "remove by entry id returned nothing")
        self.require(self.ctx["sku_l3"] not in _list_codes(await self.get_list(list_id)), "sku_l3 not removed")

    async def case_add_list_to_cart(self) -> None:
        list_id = self.ctx.get("list_a")
        self.require(list_id, "list A missing")
        before = _cart_map(await self.get_cart())
        r = await self.call("add_shopping_list_to_cart", {"list_id": list_id})
        result = r["result"]
        all_added = result.get("allAddedToCart")
        if all_added is None and isinstance(result.get("data"), dict):
            all_added = result["data"].get("allAddedToCart")
        self.require(all_added is True, f"bulk add failed: {r}")
        after = _cart_map(await self.get_cart())
        added = set(after) - set(before)
        self.require(added, "no cart entry appeared after bulk add")
        for code in added:
            self.created_cart_entries.append(str(after[code]["entryNumber"]))
        self.require(self.ctx["sku_l2"] in after, "expected sku_l2 in cart after bulk add")

    async def case_delete_lists(self) -> None:
        list_a = self.ctx.get("list_a")
        list_b = self.ctx.get("list_b")
        self.require(list_a and list_b, "lists missing")
        await self.call("delete_shopping_list", {"list_id": list_a})
        await self.call("delete_shopping_list", {"list_id": list_b})
        ids = await self.list_ids()
        self.require(list_a not in ids, "list A still present after delete")
        self.require(list_b not in ids, "list B still present after delete")
        for lid in (list_a, list_b):
            if lid in self.created_lists:
                self.created_lists.remove(lid)

    async def case_validation_errors(self) -> None:
        r = await self.raw("remove_from_shopping_list", {"list_id": "0"})
        self.require(r.get("error"), "remove with no selector should error")
        r = await self.raw("update_shopping_list_item", {"list_id": "0", "quantity": 1})
        self.require(r.get("error"), "update with no selector should error")
        r = await self.raw("move_shopping_list_items", {"from_list_id": "0", "to_list_id": "1"})
        self.require(r.get("error"), "move with no selector should error")

    async def case_bogus_list_handled(self) -> None:
        # An unknown list id must be handled gracefully (error dict, no crash).
        r = await self.raw("get_shopping_list", {"list_id": "0000000000000"})
        self.require(isinstance(r, dict), "bogus get_shopping_list did not return an object")

    async def case_not_logged_in_paths(self) -> None:
        # A bogus user_token should still be rejected without breaking the process.
        r = await self.raw("get_cart", {"user_token": "not-a-token"})
        self.require(isinstance(r, dict), "bogus token get_cart did not return an object")

    async def case_optional_params(self) -> None:
        # country override on a product tool
        r = await self.call("get_product", {"sku": self.ctx["sku_product"], "country": "nz"})
        self.require(r["country"] == "Bunnings NZ", "country override not honoured on get_product")
        # explicit user_token override on list tools
        token = get_user_token(get_country_config("au"))
        self.require(token, "no user token to pass")
        lists = await self.call("list_shopping_lists", {"user_token": token})
        self.require("wishLists" in lists["lists"], "explicit user_token list_shopping_lists failed")
        real_list = self.ctx.get("real_list")
        if not real_list:
            raise SkipTest("no pre-existing list to read with an explicit token")
        detail = await self.call("get_shopping_list", {"list_id": real_list, "user_token": token})
        self.require(detail["list"].get("name"), "explicit user_token get_shopping_list failed")

    # ---- runner ----------------------------------------------------------

    async def run(self) -> None:
        await self.setup()
        sections: list[tuple[str, list]] = [
            ("Authentication", [
                ("login", self.case_login()),
                ("check_login_status", self.case_check_login_status()),
                ("complete_login", self.case_complete_login()),
                ("logout + restore", self.case_logout_and_restore()),
            ]),
            ("Products (read-only)", [
                ("search_products", self.case_search_products()),
                ("search_products options", self.case_search_options()),
                ("get_product", self.case_get_product()),
                ("get_product_price", self.case_get_product_price()),
                ("get_product_stock", self.case_get_product_stock()),
                ("get_product_location", self.case_get_product_location()),
                ("browse_category", self.case_browse_category()),
                ("list_stores", self.case_list_stores()),
            ]),
            ("Cart", [
                ("get_cart", self.case_get_cart()),
                ("get_cart user_token", self.case_get_cart_user_token()),
                ("add/update/remove cart", self.case_add_update_remove_cart()),
            ]),
            ("Project lists", [
                ("create_shopping_list", self.case_create_list()),
                ("add_to_shopping_list", self.case_add_items()),
                ("duplicate add rejected", self.case_duplicate_add_rejected()),
                ("update qty (item_number)", self.case_update_qty_by_item_number()),
                ("update qty (entry id)", self.case_update_qty_by_entry_id()),
                ("rename_shopping_list", self.case_rename_list()),
                ("create dest + move (item_numbers)", self.case_create_dest_and_move()),
                ("move back (entry ids)", self.case_move_back_by_entry_id()),
                ("set_active_shopping_list", self.case_set_active()),
                ("remove (item_number)", self.case_remove_item_by_item_number()),
                ("remove (entry id)", self.case_remove_item_by_entry_id()),
                ("add_shopping_list_to_cart", self.case_add_list_to_cart()),
                ("delete_shopping_list", self.case_delete_lists()),
            ]),
            ("Validation", [
                ("missing-selector errors", self.case_validation_errors()),
                ("bogus list handled", self.case_bogus_list_handled()),
                ("bogus token handled", self.case_not_logged_in_paths()),
                ("optional params (country/token)", self.case_optional_params()),
            ]),
        ]
        for _section, cases in sections:
            for name, coro in cases:
                await self.run_case(name, coro)


async def main() -> int:
    verification: list[tuple[str, str, str]] = []
    async with Client(server.mcp) as client:
        suite = Suite(client)
        try:
            await suite.run()
        finally:
            print("\n=== cleanup ===")
            await suite.cleanup()
            verification = await suite.verify_restored()

    passed = failed = skipped = 0
    print("\n=== results ===")
    for name, status, detail in suite.results:
        if status == "PASS":
            passed += 1
        elif status == "FAIL":
            failed += 1
        else:
            skipped += 1
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
        line = f"  [{mark}] {name}"
        if detail and status != "PASS":
            line += f"  -- {detail}"
        print(line)
    print("\n=== end state ===")
    for name, status, detail in verification:
        if status == "FAIL":
            failed += 1
            print(f"  [FAIL] {name}  -- {detail}")
        else:
            print(f"  [{status}] {name}")
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
