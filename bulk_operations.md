# Bulk Operations — Design Reference

## Publive API Overview

### CDS (Content Delivery Service)
- Read-only, 1000 req/min
- Posts: listing, details by ID, details by URL, trending
- Categories & Tags: listing + details
- Authors: listing + details
- Navigation: navbar, footer, active slots
- Sitemaps: all-content, news, category, paginated, webcontent, webstory
- Publisher config, content types, newsletter groups, live blog updates, form schema
- Static files: robots.txt, ads.txt, service worker, push notification HTML

### CMS (Content Management Service)
- Full CRUD, 200 req/min
- **Posts** — create, retrieve, list, update (PATCH), delete
- **Categories** — full CRUD
- **Tags** — full CRUD
- **Media Library** — full CRUD
- **Live Blog Updates** — full CRUD (scoped to a live blog post)
- **Custom Components** — full CRUD
- **Custom Content Types** — full CRUD
- **Forms** — submission only

---

## Why No Native Bulk Endpoints

The Publive API has no native bulk endpoints — every operation targets a single resource (one post per PATCH, one tag per DELETE, etc.). Bulk must be orchestrated client-side by fanning out individual calls.

---

## Implementation Pattern

1. **List** — call the list endpoint with `limit=50` (max) to collect all target IDs, paginating through all pages as needed.
2. **Fan out** — fire individual PATCH/DELETE calls per ID, parallelised with a thread pool.
3. **Rate-gate** — CMS limit is 200 req/min (~3.3/sec), so cap concurrency (`max_workers=3`) and apply exponential backoff on 429 responses.
4. **Dry-run first** — collect and show what *would* change before committing. Matches the existing tier-2/3 pattern in `cms_tools.py`.
5. **Per-item results** — return a summary: how many succeeded, which IDs failed and why.

### Example flow: bulk publish 500 drafts

```
bulk_update_posts(ids=[...], patch={"status": "Published"}, dry_run=True)

1. Page through list endpoint to validate all IDs exist
2. If dry_run=True:
     → return preview: "500 posts would be set to Published"
3. If dry_run=False:
     → ThreadPoolExecutor(max_workers=3)
     → PATCH /post/{id}/ for each ID
     → exponential backoff on 429
     → collect {id, success/error} per item
     → return summary: {succeeded: 498, failed: [{id: 123, error: "..."}, ...]}
```

---

## Candidate Bulk Tools

| Tool | Tier | Dry-run | Underlying call |
|---|---|---|---|
| `bulk_update_posts` | Tier 3 | Yes | `PATCH /post/{id}/` per ID |
| `bulk_delete_posts` | Tier 3 | Yes + confirm_delete | `DELETE /post/{id}/` per ID |
| `bulk_tag_posts` | Tier 3 | Yes | `PATCH /post/{id}/` (tags field) per ID |
| `bulk_update_tags` | Tier 3 | Yes | `PATCH /tag/{id}/` per ID |
| `bulk_update_categories` | Tier 3 | Yes | `PATCH /category/{id}/` per ID |

All tools follow the same shape:
- Accept a list of IDs + the change to apply
- `dry_run=True` by default — returns a preview without writing
- `dry_run=False` executes; for deletes also requires `confirm_delete=True`
- Returns per-item success/failure in the response

---

## Rate Limit Constraints

| Service | Limit | Window |
|---|---|---|
| CDS | 1000 requests | Per minute |
| CMS | 200 requests | Per minute |

With `max_workers=3` and a 1s delay between batches, operations stay comfortably under the 200 req/min CMS ceiling even for large workloads.

Responses include headers to monitor headroom:
- `X-RateLimit-Remaining` — requests left in current window
- `X-RateLimit-Reset` — Unix timestamp when the window resets

---

## Where to Implement

- Handler functions go in `mcp_app/cms_tools.py`, appended to `CMS_TOOLS`
- HTTP calls use `mcp_app/cms_client.py` (`cms_patch`, `cms_delete`)
- No changes needed in `mcp_app/views.py` — dispatch is data-driven
