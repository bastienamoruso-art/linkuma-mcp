# Linkuma MCP

> Safe, idempotent netlinking automation for the [Linkuma](https://app.linkuma.com/) API.
> Built-in dry-run, anchor validation, budget cap, and a local-citation campaign builder.
> [Model Context Protocol](https://modelcontextprotocol.io/) server — works with Claude Desktop, Claude Code, and any MCP-aware client.

---

## Why this MCP

Wrapping a paid API in 200 lines of `httpx` is easy. Doing it without ever double-billing your card or shipping a campaign with the wrong anchor is the hard part. This server ships with three things you won't write yourself the first time:

1. **Idempotence by default.** Every order is keyed by an `external_ref`. A cache at `~/.linkuma-mcp/idempotency.json` is checked _before_ every `POST /carts/order`, and an opportunistic server-side lookup runs after. A timeout or 5xx is never silently retried — you get a typed `LinkumaOrderUncertain` with a recovery path.
2. **Dry-run, then confirm.** Pricing and campaign-planning tools issue a single-use `confirm_token` valid for 10 minutes. Nothing leaves your machine until you hand it back.
3. **Business guardrails the docs don't mention.** Anchor over-optimisation across a batch and across 90-day history. `pagekw` vs anchor coherence. Hard budget cap from `LINKUMA_BUDGET_CAP_EUR`. Refusal of short Google Maps URLs (`maps.app.goo.gl`, `g.co/maps`) that produce broken local citations.

---

## Install

```bash
pip install linkuma-mcp
```

Or from source:

```bash
git clone https://github.com/bastienamoruso-art/linkuma-mcp.git
cd linkuma-mcp
pip install -e ".[dev]"
```

Then create a `.env` from `.env.example` and set at least `LINKUMA_API_KEY`.

### Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "linkuma": {
      "command": "linkuma-mcp",
      "env": {
        "LINKUMA_API_KEY": "lkm_xxxxxxxxxxxxxxxxxxxxxxxxx",
        "LINKUMA_BUDGET_CAP_EUR": "300"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add linkuma -- linkuma-mcp
# or, for a project-scoped install:
claude mcp add linkuma --scope project -e LINKUMA_API_KEY=lkm_... -- linkuma-mcp
```

---

## Quickstart

### 1. Health check

> Always call this first. It tells you the key works, your current credit, and prints a quickstart Markdown.

```text
> Use linkuma_doctor
{
  "api_key_valid": true,
  "credit_eur": 412.5,
  "low_credit_warning": false,
  "projects_count": 6,
  "recent_orders_30d": 14,
  "quickstart_md": "## Linkuma MCP — quickstart..."
}
```

### 2. Place a single editorial link

```text
> Price a single premium link for project p_abc to https://my-site.fr/services/courtier with anchor "courtier orleans".

# Tool call: linkuma_cart_price
{
  "project_id": "p_abc",
  "items": [{
    "tier": "premium",
    "thematic_id": "5d13d4ff-4204-4035-b8b4-f96280f2babe",
    "target_url": "https://my-site.fr/services/courtier",
    "anchor": "courtier orleans",
    "pagekw": "courtier immobilier orleans"
  }]
}

# Response includes a confirm_token. Then:

> Place the order, idempotency ref "courtier-orleans-2026-05-11".

# Tool call: linkuma_cart_order
{ "confirm_token": "lkm_cnf_...", "external_ref": "courtier-orleans-2026-05-11" }
```

If the call times out, you get `LinkumaOrderUncertain` — do **not** retry. Instead:

```text
> Did the order go through?

# Tool call: linkuma_orders_list
{ "external_ref": "courtier-orleans-2026-05-11", "limit": 5 }
```

### 3. Local citation campaign

```text
> Build a 10-citation campaign for Le Petit Bistrot Marseille pointing to https://lepetitbistrot-marseille.fr/, GMaps URL https://www.google.com/maps/place/Le+Petit+Bistrot/@43.2965,5.3698,17z, budget 250 EUR, thematic hint "travaux".

# Tool: linkuma_local_campaign_plan
# -> returns 10 items with publish dates spread over 30 days,
#    a recommended thematic + 3 alternatives,
#    a confirm_token.

> Execute it.

# Tool: linkuma_local_campaign_execute
# -> credit is re-checked before EACH item.
#    A partial failure mid-batch returns `orders_failed[]`
#    with `reason: "insufficient_credit"` and you can
#    re-run the same plan: items already shipped are deduped.
```

---

## Tools reference

| Tool | What it does | Safety notes |
|---|---|---|
| `linkuma_doctor` | Verify API key, return credit + quickstart | Read-only |
| `linkuma_projects_list` | List projects with optional name filter | Read-only |
| `linkuma_projects_create` | Create project or return existing one (dedup by exact name) | Write — idempotent on `name` |
| `linkuma_thematics_list` | Thematics for a tier (`basic`/`standard`/`premium`) | Cached 1 h |
| `linkuma_cart_price` | Price + run all checks (URL status, anchors, pagekw, budget, credit) | Read-only — issues a single-use `confirm_token` |
| `linkuma_cart_order` | Commit a priced cart | Triple-layer dedup. Zero auto-retry. Budget cap enforced |
| `linkuma_orders_list` | List orders (filter by project / status / since / `external_ref`) | Read-only |
| `linkuma_orders_get` | Full detail of one order | Read-only |
| `linkuma_local_campaign_plan` | Dry-run plan for N citations with thematic proposal | Read-only |
| `linkuma_local_campaign_execute` | Place the plan — per-item idempotency on `{plan_id}-{idx}` | Stops on first credit shortage |

### Resources (read-only views)

- `linkuma://settings/credit` — JSON snapshot of your settings (credit balance).
- `linkuma://projects` — current list of projects.
- `linkuma://thematics` — `premium` thematics for `lang=fr`, suitable for local citations.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `LINKUMA_API_KEY` | — _(required)_ | Bearer token for the API. |
| `LINKUMA_BASE_URL` | `https://app.linkuma.com/api/v1` | Override for staging or tests. |
| `LINKUMA_BUDGET_CAP_EUR` | `500` | Hard cap before any order POST. Blocks the order if exceeded. |
| `LINKUMA_RATE_LIMIT_RPS` | `2` | Client-side rate limit (token bucket). |
| `LINKUMA_LOW_CREDIT_THRESHOLD_EUR` | `50` | `linkuma_doctor` flags `low_credit_warning=true` below this. |
| `LINKUMA_IDEMPOTENCY_DIR` | `~/.linkuma-mcp` | Where the JSON dedup store lives. |
| `LINKUMA_LOG_LEVEL` | `INFO` | Python logging level. |

---

## Architecture notes

The flow is always **plan → confirm → execute**:

1. `cart_price` / `local_campaign_plan` return a single-use `confirm_token` (in-memory, 10-min TTL).
2. `cart_order` / `local_campaign_execute` consume that token. No token, no write.
3. Every order POST is keyed by `external_ref` and checked against `~/.linkuma-mcp/idempotency.json` _before_ leaving your machine. Replays are no-ops.
4. The HTTP client _never_ retries `POST /carts/order` on a 5xx, timeout, or network error. It raises `LinkumaOrderUncertain`. The recovery path is documented in every error message: `linkuma_orders_list(external_ref=...)`.

The local store is JSON, file-locked (`fcntl.flock` on POSIX), and survives crashes. It is the authoritative source-of-truth for "did I already ship this `external_ref`?".

---

## Roadmap v2

- Multi-account support via `LINKUMA_API_KEYS_JSON` and an `account` param on every tool.
- `linkuma_editorial_campaign_plan` / `_execute` — symmetric to the local one.
- `linkuma_suggest_tier(target_url, anchor, budget, context)` — pricing intelligence.
- `linkuma_export_orders` — CSV + Google Sheets sync.
- `linkuma_post_mortem` — GSC × Linkuma lift attribution (requires GSC OAuth or Cuik MCP).
- `linkuma_benchmark_competitors` — reference profiles via Babbar / DataForSEO.
- Optional Notion sync for tracked orders.

---

## Contributing

```bash
git clone https://github.com/bastienamoruso-art/linkuma-mcp.git
cd linkuma-mcp
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

PRs welcome — please keep tests green and run `ruff` before submitting.

---

## Credits

Built by [Bastien Amoruso](https://github.com/bastienamoruso-art) as a sibling to [seo-agent-claude](https://github.com/bastienamoruso-art/seo-agent-claude) — the agent toolkit that drives my freelance SEO workflows. MIT licensed; use it, fork it, ship things that don't double-bill your card.
