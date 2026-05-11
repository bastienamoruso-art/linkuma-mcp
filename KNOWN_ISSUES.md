# Known issues

Tracker for upstream constraints and remaining gotchas. v0.3.0 fixed the
major blockers from v0.2.1 (cart body schema). Anything left here is either
upstream behavior we can't fix from the MCP side, or notes for future
releases.

---

## ✅ Fixed in v0.3.0

### `/carts/price` and `/carts/order` — body schema mismatch

**Status**: ✅ Fixed. The MCP body now matches the real Linkuma schema,
reverse-engineered from 10 successful production orders (Maisons Elytis,
2026-05-11). Live `linkuma_cart_price` confirmed returning `total_price: 35`
for a `citation_boost` item.

The cart item schema (v0.3.0):

```jsonc
{
  "type": "citation_boost",       // basic | standard | premium | citation_linkuma | citation_boost
  "url": "https://example.fr/",
  "map": "https://www.google.com/maps/place/...",  // required for citation_*
  "project_id": "uuid",
  "thematic_id": "uuid",
  "category_id": "uuid",            // optional; required by some citation flows
  "qty": 1,
  "anchor": "custom",               // url | generic | custom
  "anchor_value": "ancre1, ancre2", // required when anchor=="custom"
  "distribution": "direct",         // direct | schedule
  "started_at": "2026-05-25",       // must be >= J+4 working days
  "fast_publication": false,
  "brief": "text",                  // editorial/citation brief
  "pagekw": "main keyword"          // required when type is editorial (basic/standard/premium)
}
```

Top-level cart body:
- `/carts/price` → `{ "items": [...] }`
- `/carts/order` → `{ "items": [...], "payment_method": "direct_credits",
                       "external_ref": "...", "nice_name": "..." }`

---

## Upstream constraints (not MCP bugs)

### `started_at` must be J+4 working days minimum

Linkuma rejects `started_at` if it is earlier than four working days from
today. The plan tools (`linkuma_local_campaign_plan`,
`linkuma_editorial_campaign_plan`) default the first publication date to
J+5 working days as a safe buffer. If you build items manually via
`linkuma_cart_price`, you must respect this constraint or you'll get
`"La date de publication doit être à J+4 ouvré minimum"`.

### Thematic catalogue varies by item `type`

`GET /thematics/{tier}` returns the thematics available for that tier, but
the pool of *publishers* attached to each thematic for a given combination
of `(thematic_id, project_id, type)` may be empty. In that case Linkuma
returns:

```
"Thématique non disponible pour ce type de commande"
"Le nombre de sites disponibles pour vos thématique / projet / type de commande est 0"
```

There is no MCP-side fix — you have to pick a thematic with available
inventory. `linkuma_local_campaign_plan` exposes `thematic_alternatives` so
the agent can retry with another thematic when the first one is empty.

### `pagekw` required for editorial items

For `type in {basic, standard, premium}`, `pagekw` is required upstream.
The MCP does not block locally when it is missing (Linkuma will reject the
request with a clear 422), but `linkuma_editorial_campaign_plan` exposes a
`pagekw_per_target` parameter — pass it.

### `/carts` upstream filters not honoured

`?status=`, `?project_id=`, `?since=` on `GET /carts` are ignored
server-side. The MCP applies filters client-side after the fetch — see
`LinkumaClient.list_orders`. No action required.

---

## `/thematics` — path vs query

`GET /thematics/{tier}` (tier in the path, e.g. `/thematics/premium`).
There is no `?tier=` query parameter — the MCP already uses the right URL.

---

## Order schema differences API vs MCP spec

The API returns some short-form fields not documented in the MCP spec.
`LinkumaClient.list_orders` and `get_order` apply the following
normalisation:

| API field        | Normalised MCP field |
|------------------|----------------------|
| `id`             | `order_id` (kept as `id` too) |
| `url`            | `target_url` (back-compat) |
| `type`           | `tier` (legacy alias) |
| `price`          | `price_eur`          |
| cart-level `id`  | `cart_id`            |
| cart `nice_name` | `external_ref` (fallback) |

These are response normalisations only; request bodies use the v0.3.0
schema documented above.

---

## Order `project_id` enrichment

Orders embedded in `/carts` don't carry `project_id`. The dashboard/export
tools back-fill it via `/projects` (which exposes `orders[]` per project).
No user-facing impact.
