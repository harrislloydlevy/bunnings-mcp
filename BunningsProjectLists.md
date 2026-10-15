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

> **Region header gotcha.** Wishlist requests send `X-region: <region>` (e.g.
> `NSWMetro`). The bulk add-to-cart service (`/_apis/lstexp/...`) instead requires a
> plain **`region`** header — sending only `X-region` there returns
> `400 "The Region field is required."`.

## Verified operations

| Operation | Method + path | Body | Response |
|---|---|---|---|
| List lists | `GET /wishlist?sortOrder=default&fields=SLIM` | — | `data.wishLists[]` |
| Create list | `POST /wishlist` | `{"name":"My List"}` | 201, `data.wishlistId` + full list |
| Get list detail | `GET /wishlist/{id}` | — | `data` incl. `entries[]`, `productsCount` |
| Add item | `POST /wishlist` | `{"code":"<sku>","list":"","name":"<list name>","productCategory":"","productName":"","productValue":0,"quantity":"1","wishlistId":"<id>","wishlistName":"<list name>"}` | 201 |
| Rename list | `PATCH /wishlist/{id}` | `{"name":"New Name"}` | 200, full list |
| Change item quantity | `PATCH /wishlist/{id}` | `{"entries":[{"wishlistEntryId":"<eid>","quantity":"2"}]}` | 200, full list |
| Remove item(s) | `DELETE /wishlist/{id}/removeEntry` | `{"entries":[{"productId":"<sku>","wishlistEntryId":"<eid>"}]}` | 200, **empty body** |
| Set active list | `POST /wishlist/{id}/setActive` | — | 200, full `wishLists` |
| Move item(s) between lists | `POST /wishlist/{TARGET_ID}/moveProducts` | `{"entries":[{"quantity":"2","wishlistEntryId":"<eid>"}]}` | 200 |
| Delete list | `DELETE /wishlist/removeWishlist` | `{"wishLists":[{"wishlistId":"<id>"}]}` | 200, remaining `wishLists` |
| **Add whole list → cart** | `POST /_apis/lstexp/list/addToCart` | `{"cartId":"<cartId>","items":[{"code":"<sku>","quantity":1}]}` | 200, `data:{allAddedToCart, failedItems}` |
| Share / email list | `POST /wishlist/{id}/sendEmail` | `{"captchaResponse":"<reCAPTCHA Enterprise token>","recipientEmail":"<to>","recipientName":"<name>"}` | 200 |

### Key details

- **Entry identity.** Every entry in `GET /wishlist/{id}` carries a `wishlistEntryId`
  (e.g. `9948548039358`) that is distinct from the product `code`. **All mutating calls
  use `wishlistEntryId`, not `code`.** The top-level entry fields are
  `addedDate, productCost, quantity, wishlistEntryId` plus the nested `product`.
- **Add item requires a non-empty list name.** Omitting `wishlistName` (or sending it
  empty) returns `200` with `"Wishlisht name should not be empty."` (sic) and adds
  nothing. `name` should match too. Cosmetic fields (`productCategory`, `productName`,
  `productValue`, `list`) may be empty/zero.
- **`removeEntry`** accepts `{"entries":[{"wishlistEntryId":"<eid>"}]}`; the canonical
  browser body also includes `productId`. Responds `200` with an **empty body**
  (the client must tolerate empty responses). Sending only `{"product":{"code":"<sku>"}}`
  returns `400`.
- **`moveProducts`**: the id in the **path is the destination (target) list**; the source
  is inferred from the entry ids. (Confirmed via the leaked signature before; not present
  in the browser HAR because the UI has no "move to list" affordance.)
- **`removeWishlist`**: body is the plural DTO `{"wishLists":[{"wishlistId":"<id>"}]}`.
  A single `wishlistId`/`id`/array body returns 200 **with no effect** (silent no-op).
- **Duplicate add is a no-op**: adding a SKU already in the list adds nothing and does not
  create a second entry. Use `PATCH ... entries` to set quantity.
- **Quantity** is sent as a string (`"2"`) on add and on `PATCH`.

## Add whole list to cart — the real endpoint

The wishlist controller exposes `POST /wishlist/{id}/addToCart`, but **the web app never
calls it** and every body shape returns `BUS-4018 "Cart model is empty for the given cart
id"`. The actual "add all to cart" is a **separate `lstexp` service**:

```
POST /_apis/lstexp/list/addToCart
region: <region>            # plain header, REQUIRED
Content-Type: application/json

{"cartId":"W292130733","items":[{"code":"0049052","quantity":1}]}

-> 200 {"statusDetails":{"state":"SUCCESS"},"data":{"allAddedToCart":true,"failedItems":null}}
```

The list id is **not** in the request — the client resolves the current list's items and
sends `{code, quantity}` pairs plus the current cart id
(from `POST /_apis/v1/carts?oldCartId=&fields=LITE&cartCreate=false`, field `data.code`).

## Share / email a list

```
POST /_apis/v1/customer-management/users/wishlist/{id}/sendEmail
{"captchaResponse":"0cAFcWeA...","recipientEmail":"me@example.com","recipientName":"Me"}
```

`captchaResponse` is a **reCAPTCHA Enterprise** token minted by the browser, so this
cannot be driven server-side without solving a captcha. Not exposed as an MCP tool; the
client function exists for completeness.

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

Unlike most routes, the list-mutation endpoints are **not** in the frontend's static
config map — they are injected per-page by Sitecore as `params` in the page HTML:

```
projectListEndPoint      = /v1/customer-management/users/wishlist
removeProductsEndPoint   = /v1/customer-management/users/wishlist/{wishlistId}/removeEntry
moveProductsEndPoint     = /v1/customer-management/users/wishlist/{wishlistId}/moveProducts
addToCartEndPoint        = /v1/customer-management/users/wishlist/{wishlistId}/addToCart   (unused by web app)
getSpecificProjectListEndPoint = /v1/customer-management/users/wishlist/{wishlistId}
shareProjectList         = /v1/customer-management/users/wishlist/{wishListId}/sendEmail   (static config map)
removeProjectList        = /v1/customer-management/users/wishlist/removeWishlist           (static config map)
addToCartBulk            = /lstexp/list/addToCart                                           (static config map — the real one)
```

The frontend callers live in `/_next/static/chunks/pages/_app-*.js`. `moveProducts`
replaces `{wishlistId}` with `newListId` (the destination) and posts
`{entries: entries.map(e => ({quantity: e.quantity, wishlistEntryId: e.wishlistEntryId}))}`.

## How this was verified

Live probe scripts (throwaway, in `/tmp/opencode/`) drove the real API with a valid
session, then an end-to-end test drove the **MCP server tools** themselves: created
throwaway lists, added/removed/renamed/moved items, changed quantities, set active,
added a list to the cart, and deleted the lists. All test lists and the injected cart
entry were removed afterwards.

> Implementation note: `@mcp.tool()` rebinds the decorated name in `bunnings_mcp.server`,
> so a tool must **not** call an imported client function of the same name (it would
> capture the tool itself and return a coroutine). The server imports the client module
> and calls `client.<fn>(...)`.
