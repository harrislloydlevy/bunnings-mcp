# Bunnings MCP Server

A FastMCP server for accessing Bunnings APIs — product search, stores, pricing, stock, cart, and shopping lists.

Complete and utter vibe-coded nonsense.Has worked for me, but may not for you. In fact I'd be surprised.

## Features

- **Product Search** — Search the Bunnings catalog by keyword
- **Product Details** — Full product info including description, features, images
- **Pricing** — Current prices for any product
- **Stock & Fulfillment** — In-store, Click & Collect, and delivery availability
- **Store Locator** — List all stores with addresses, hours, and services
- **Aisle/Bay Locations** — Find where products are in the store
- **Cart Management** — Add, update, remove items (requires user auth)
- **Shopping Lists** — Create and manage project lists (requires user auth)

## Quick Start

### Install

```bash
pip install -e .
```

### Run

```bash
bunnings-mcp
```

### Docker

```bash
docker build -t bunnings-mcp .
docker run -p 8000:8000 bunnings-mcp
```

## Tools

### Product Tools (No Auth Required)

| Tool | Description |
|------|-------------|
| `search_products` | Search products by keyword |
| `get_product` | Get full product details by SKU |
| `get_product_price` | Get current price |
| `get_product_stock` | Check stock/fulfillment |
| `get_product_location` | Get aisle/bay location |
| `browse_category` | Browse by category path |

### Store Tools (No Auth Required)

| Tool | Description |
|------|-------------|
| `list_stores` | List all stores with details |

### Cart Tools (User Auth Required)

| Tool | Description |
|------|-------------|
| `get_cart` | Get current cart |
| `add_to_cart` | Add item to cart |
| `update_cart_item` | Update item quantity |
| `remove_from_cart` | Remove item from cart |

### Shopping List Tools (User Auth Required)

| Tool | Description |
|------|-------------|
| `list_shopping_lists` | List all project lists |
| `create_shopping_list` | Create a new list |
| `get_shopping_list` | Get list with items |
| `add_to_shopping_list` | Add item to list |
| `remove_from_shopping_list` | Remove item from list |
| `delete_shopping_list` | Delete a list |

## Authentication

Product search, details, pricing, stock, and store locations use a **guest token** that is automatically minted via Bunnings' public OAuth2 flow — no login required.

Cart and shopping list tools require an **authenticated user token**. To get one:
1. Log in to [bunnings.com.au](https://www.bunnings.com.au) or [bunnings.co.nz](https://www.bunnings.co.nz)
2. Open browser DevTools → Network tab
3. Find any `/_apis/` request and copy the `Authorization: Bearer <token>` value
4. Pass that token as the `user_token` parameter

## API Research

This server is built from:
- [Bunnings Developer Portal](https://developer.live.bunnings.com.au/) — official API docs
- [Unofficial API notes](https://github.com/thecolab-ai/.skills/blob/main/skills/bunnings/references/api-notes.md) — reverse-engineered endpoints
- Direct analysis of bunnings.com.au website traffic

### Key Endpoints

- `POST /_apis/v1/coveo/search` — Product search (Coveo)
- `GET /_apis/v1/products/{sku}?fields=FULL` — Product details
- `GET /_apis/v2/products/{sku}/priceInfo` — Pricing
- `POST /_apis/v2/products/{sku}/fulfillment` — Stock/fulfillment
- `GET /_apis/v1/item-api/locations` — Aisle/bay locations
- `GET /_apis/v1/stores/country/{AU|NZ}?fields=FULL` — Store list
- `/_apis/v1/basket` — Cart operations (auth required)
- `/_apis/v1/projectlists` — Shopping/project lists (auth required)

## License

MIT
