# Known issues (v0.2.1)

Bugs observés contre l'API Linkuma de production qui ne sont **pas encore fixés**
dans cette release (ils demandent un refactor non-trivial des body builders et
des tests). Tracker pour la prochaine release.

---

## `/carts/price` and `/carts/order` — body schema mismatch

**Endpoints concernés** : `POST /carts/price`, `POST /carts/order`.

**Symptôme** : 422 Unprocessable Entity sur tout appel via les tools
`linkuma_cart_price` et `linkuma_cart_order` (et indirectement `*_campaign_plan`
qui retombent gracieusement sur une estimation locale via `_TIER_PRICE_FLOOR`).

**Cause** : le MCP envoie un body de la forme
```json
{"project_id": "...", "items": [{"tier": "premium", "thematic_id": "...", "target_url": "...", "anchor": "..."}]}
```
mais l'API exige en réalité, pour chaque item :
- `type` (catalogue: `citation_boost`, `citation_linkuma`, `basic`, `standard`, `premium`)
- `qty` (int)
- `url` (string, requis si `is_no_link != true`)
- `project_id` (au niveau de l'item, pas seulement du cart)
- `thematic_id` (doit pointer sur un id valide du tier — le pool de thematics
  varie par tier)
- `anchor` (doit faire partie d'un catalogue serveur de valeurs autorisées,
  pas une string arbitraire — `"The selected items.0.anchor is invalid"`)
- `distribution` (probablement `direct` / `dillution` ; spec à clarifier)
- `started_at` (date ISO)
- `fast_publication` (bool)
- `pagekw` (requis SAUF si `type in {citation_linkuma, citation_boost}`)

**Conséquence pour les utilisateurs** :
- `linkuma_cart_price` retourne `LinkumaValidationError` 422.
- `linkuma_cart_order` retournera 422 avant tout débit (donc pas de risque de
  perte de crédit, mais l'order ne se fera pas non plus).
- `linkuma_local_campaign_plan` et `linkuma_editorial_campaign_plan` continuent
  de fonctionner en mode "estimation locale" (warning `could not call
  /carts/price; used local estimate`) puisqu'ils ont un fallback.

**Fix prévu (v0.3.0)** :
1. Refactor `LinkumaClient.cart_price` / `cart_order` pour envoyer le body avec
   les champs exigés : remap `tier -> type`, ajouter `qty=1`, `url=target_url`,
   `distribution`, `started_at`, `fast_publication=False`.
2. Récupérer le catalogue d'`anchor` autorisées depuis le serveur (endpoint à
   identifier — probablement via `/spots` ou `/catalog`).
3. Adapter le test suite en conséquence.
4. Garder `_TIER_PRICE_FLOOR` comme estimation locale tant que cart_price n'est
   pas fiable.

---

## `/thematics` — path vs query

**Endpoint** : `GET /thematics`.

**Symptôme initial** : 404 sur `GET /thematics?tier=premium`.

**Cause** : Linkuma expose `GET /thematics/{tier}` (tier dans le path), pas
`/thematics?tier=...`. Le MCP utilisait déjà la bonne URL côté code
(`thematics.py:66 -> /thematics/{tier}`), donc **pas de bug**, juste à noter
que le query parameter `tier` n'existe pas.

**Status** : pas un bug, juste documentation.

---

## `/carts` — upstream filters not honoured

**Endpoint** : `GET /carts`.

**Symptôme** : passer `?status=refused` ou `?since=2026-02-10` à `/carts` ne
filtre pas la réponse côté serveur — l'API renvoie systématiquement le même
contenu (full liste).

**Cause** : Linkuma ne supporte probablement pas ces query parameters (testé en
prod, mêmes payloads retournés avec ou sans filtre).

**Fix appliqué (v0.2.1)** : `LinkumaClient.list_orders` applique les filtres
côté client après fetch des carts (statuts, project_id, since, external_ref,
limit).

**Status** : ✅ Fixé.

---

## Order schema differences API vs MCP spec

L'API renvoie des champs en short-form non documentés dans la "spec" qui a
servi à écrire le MCP. La couche de normalisation dans `LinkumaClient.list_orders`
fait les mappings suivants :

| API field        | Normalised MCP field |
|------------------|----------------------|
| `id`             | `order_id` (et `id` conservé) |
| `url`            | `target_url`         |
| `type`           | `tier` (`Citation Boost` → `citation_boost`, etc.) |
| `price`          | `price_eur`          |
| `status`         | `status` (valeurs courtes : `pending`, `published`, `refused`, ...) |
| cart-level `id`  | `cart_id`            |
| cart `nice_name` | `external_ref` (si pas déjà défini) |

**Status** : ✅ Fixé dans v0.2.1.

---

## Order `project_id` enrichment

Les orders renvoyés inline dans `/carts` ne portent pas de champ `project_id`.
On le récupère via un fetch de `/projects` (qui expose `orders[]` par projet),
puis on remplit `order.project_id` côté client.

**Tools enrichis** : `linkuma_dashboard`, `linkuma_export_orders`,
`linkuma_orders_refused_analyze`.

**Status** : ✅ Fixé dans v0.2.1.
