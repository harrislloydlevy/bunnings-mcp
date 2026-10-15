# Bunnings Project Lists (Wishlist) API — Verified Reference

> Living document. All requests below were exercised **live** against an authenticated AU
> session. Update whenever new findings occur. **Read this before touching list code.**

The web app calls these "**project lists**"; the backend calls them "**wishlist**".
Auth is the same cookie set as elsewhere — see [`BunningsAuth.md`](BunningsAuth.md).
Requests must send **both** `GuestAuthentication` and `UserAuthentication` cookies.

Base path: `/_apis/v1/customer-management/users/wishlist`

Common headers (all requests):
`Cookie: GuestAuthentication=<guest>; UserAuthentication=<jwt>`,
`clientId: mHPVWnzuBkrW7rmt56XGwKkb5Gp9BJMk`, `country: AU`, `currency: AUD`,
`locale: en_AU`, `locationCode: <store>`, `stream: RETAIL`, `correlationid: <uuid>`.
The browser also sends `Referer: https://www.bunnings.com.au/my-account/project-lists`.

## Verified operations

| Operation | Method + path | Body | Response |
|---|---|---|---|
| List lists | `GET /wishlist?sortOrder=default&fields=SLIM` | — | `data.wishLists[]` |
| Create list | `POST /wishlist` | `{"name":"My List"}` | 201, `data.wishlistId` |
| Get list detail | `GET /wishlist/{id}` | — | `data` incl. `entries[]`, `productsCount` |
| Add item | `POST /wishlist` | `{"code":"<sku>","list":"","name":"<list name>","productCategory":"","productName":"","productValue":0,"quantity":"1","wishlistId":"<id>","wishlistName":"<list name>"}` | 201 |
| Rename list | `PATCH /wishlist/{id}` | `{"name":"New Name"}` | 200 |
| Change item quantity | `PATCH /wishlist/{id}` | `{"entries":[{"wishlistEntryId":"<eid>","quantity":N}]}` | 200 |
| Remove item(s) | `DELETE /wishlist/{id}/removeEntry` | `{"entries":[{"wishlistEntryId":"<eid>"}]}` | 200 |
| Set active list | `POST /wishlist/{id}/setActive` | — | 200 |
| Move item(s) between lists | `POST /wishlist/{TARGET_ID}/moveProducts` | `{"entries":[{"quantity":N,"wishlistEntryId":"<eid>"}]}` | 200 |
| Delete list | `DELETE /wishlist/removeWishlist` | `{"wishLists":[{"wishlistId":"<id>"}]}` | 200 |

### Key details

- **Entry identity.** Every entry in `GET /wishlist/{id}` carries a `wishlistEntryId`
  (e.g. `9948548039358`) that is distinct from the product `code`. **All mutating calls
  use `wishlistEntryId`, not `code`.**
- **`removeEntry`**: `{"entries":[{"wishlistEntryId":"<eid>"}]}` works; sending only
  `{"product":{"code":"<sku>"}}` returns `400 "Parameters in input are not of correct type
  or are missing mandatory params."` Multiple entries can be removed in one call.
- **`moveProducts`**: the id in the **path is the destination (target) list**. The source
  is inferred from the entry ids. This was non-obvious and initially guessed wrong.
- **`removeWishlist`**: body is the plural DTO `{"wishLists":[{"wishlistId":"<id>"}]}`.
  A single `wishlistId`/`id`/array body returns 200 **with no effect** (silent no-op).
- **Duplicate add is a no-op**: adding a SKU already in the list returns 200 but does not
  increase quantity and does not create a second entry. Use `PATCH ... entries` to set qty.
- **Quantity** is sent as a string (`"1"`) on add; `PATCH` accepts a bare integer or string.

## Controller signatures (leaked via argument-type-mismatch errors)

Sending a JSON **array** as the request body makes Spring return `SYS-4499`
`argument type mismatch` with the full Java method signature. Very useful for discovery.

```
BunningsWishListController.removeWishlistEntry(String, Wishlist2WsDTO, String) -> void
BunningsWishListController.moveProducts(String, Wishlist2WsDTO, String, req, resp) -> WishListsWsDTO
BunningsWishListController.addToCart(String, Wishlist2WsDTO, String, req, resp) -> AddToCartValidationDataListWsDTO
BunningsWishListController.removeWishlist(String, WishListsWsDTO) -> ...
```

The trailing `String` argument on all of the above is the standard **OCC `fields` parameter**
(request param, `defaultValue="DEFAULT"`). On `removeEntry`/`moveProducts` it can be omitted.
Sending a wrong-typed body also echoes the *current argument values*, which confirmed this.

## DTO shapes (from the frontend Zod schemas)

- `Wishlist2WsDTO` (a single list):
  `{creationtime, defaultWishlist, entries:[{addedDate, position, powerPassPrice, priceType,
  product, productCost, quantity, wishlistEntryId}], isActive, isClickAndCollect, isDelivery,
  modifiedtime, name, productsCount, user:{name,uid}, wishlistId, wishlistTotalPrice}`
- `WishListsWsDTO`: `{wishLists:[{wishlistId}]}`
- `AddToCartValidationDataListWsDTO` (add-to-cart response):
  `{allAddedToCart, productDetails:[{productCode, wishlistEntryId}]}`

## Where the endpoints come from

Unlike most routes, the three list-mutation endpoints are **not** in the frontend's static
config map — they are injected per-page by Sitecore as `params` in the page HTML:

```
projectListEndPoint      = /v1/customer-management/users/wishlist
removeProductsEndPoint   = /v1/customer-management/users/wishlist/{wishlistId}/removeEntry
moveProductsEndPoint     = /v1/customer-management/users/wishlist/{wishlistId}/moveProducts
addToCartEndPoint        = /v1/customer-management/users/wishlist/{wishlistId}/addToCart
getSpecificProjectListEndPoint = /v1/customer-management/users/wishlist/{wishlistId}
shareProjectList         = /v1/customer-management/users/wishlist/{wishListId}/sendEmail   (static config map)
removeProjectList        = /v1/customer-management/users/wishlist/removeWishlist           (static config map)
```

The frontend callers live in `/_next/static/chunks/pages/_app-*.js`. `moveProducts`
replaces `{wishlistId}` with `newListId` (the destination) and posts
`{entries: entries.map(e => ({quantity: e.quantity, wishlistEntryId: e.wishlistEntryId}))}`.

## Known unknowns

1. **Add whole list → cart** — `POST /wishlist/{id}/addToCart` returns
   `BUS-4018 "Cart model is empty for the given cart id"` for every body shape tried
   (minimal entries, entries+product, productDetails, products, with `cartId` query/header,
   `fields=FULL`). The user's cart has entries, so this is a backend quirk, not an empty cart.
   *Workaround:* iterate the list's entries and call
   `POST /_apis/v1/carts/{cartId}/entries?code=<sku>&qty=N` per item.
   Native shape needs a browser HAR of "Add all to cart" on a project list.
2. **Share / email a list** — `POST /wishlist/{wishListId}/sendEmail`. Not tested
   (would send a real email). Needs a HAR.

## How this was verified

Live probe scripts (throwaway, in `/tmp/opencode/wishlist_probe*.py`) drove the real API
with a valid session: created test lists, added/removed/renamed/moved items, changed
quantities, and deleted lists; validated effects by re-reading `GET /wishlist/{id}`.
All test lists were deleted afterwards.
