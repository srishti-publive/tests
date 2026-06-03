# Publive API Tools Reference

This document maps every MCP tool in this project to the underlying Publive API endpoint it calls, explains what the tool does, and describes the full flow from tool call to API response.

---

## Architecture Overview

```
MCP Client (Claude Desktop / Cursor)
    │
    ▼
MCP Server (this project) — views.py _dispatch()
    │
    ├── Tool name in CMS_TOOL_NAMES (editorial tools)
    │       └── dispatch_cms_tool() → cms_client.py → https://cms.thepublive.com/publisher/{id}/...
    │
    └── Everything else (delivery tools)
            └── dispatch_cds_tool() → cds_client.py → https://cds.thepublive.com/publisher/{id}/...
```

**Auth:** All API calls use HTTP Basic Auth derived from the user's credentials (resolved from a Bearer token → `OAuthToken` DB record, or from a Django session cookie).

**Publisher ID:** Embedded in the base URL path. Comes from credentials stored at login/OAuth time.

---

## Part 1 — Content Delivery Service (CDS) Tools

Read-only tools. Base URL: `https://cds.thepublive.com/publisher/{publisher_id}/`

All CDS tools call `cds_client.cds_get(credentials, path, params)` which:
1. Builds the full URL
2. Adds Basic Auth header
3. Makes a GET request with 5s timeout
4. Retries once on HTTP 408 or timeout

---

### Posts

#### `fetch_published_posts`
**API:** `GET /posts/`

Lists published posts with advanced filtering, sorting, and pagination.

**Flow:**
1. Tool receives optional filters (`type`, `categories.id`, `tags.id`, `contributors.id`, `title__contains`, etc.), `page`, and `limit`.
2. Passes them as query params to `/posts/`.
3. Returns paginated list of post objects (id, title, slug, type, categories, tags, contributors, content_html, banner_url, absolute_url, publish dates).

**Key filters:** `type__eq=Article`, `categories.id__eq=100`, `title__contains=budget`, `created_at__gte=2026-01-01`

---

#### `fetch_published_post`
**API:** `GET /posts/{identifier}/`

Fetches complete details of a single published post by ID or slug.

**Flow:**
1. Tool receives `identifier` (post ID or slug).
2. Calls `/posts/{identifier}/`.
3. Returns full post object including `content_html`, `content_json`, all metadata, SEO fields, and media.

---

#### `fetch_post_by_url`
**API:** `GET /post/?legacy_url={path}`

Fetches a post by its relative/legacy URL path. Useful for URL-based routing.

**Flow:**
1. Tool receives a `legacy_url` like `/business/union-budget-2026-12345`.
2. Calls `/post/?legacy_url={path}`.
3. Returns the same full post object as `fetch_published_post`.

---

#### `fetch_livebupdates`
**API:** `GET /posts/{post_id}/live-blog-update/`

Fetches the stream of timestamped updates for a LiveBlog post.

**Flow:**
1. Tool receives `post_id` of a LiveBlog-type post plus optional `page`/`limit`.
2. Calls `/posts/{post_id}/live-blog-update/`.
3. Returns list of update entries (id, author, title, HTML content, is_pinned, timestamps).

> Only works for posts where `type = "LiveBlog"`.

---

#### `fetch_liveblog_with_updates`
**API:** `GET /post/{post_id}/` + `GET /post/{post_id}/live-blog-updates/`

Fetches the parent LiveBlog post metadata and all its update entries in a single combined call.

**Flow:**
1. Tool receives `post_id` of a LiveBlog-type post plus optional `page`/`limit`.
2. Fetches the post details from `/post/{post_id}/`.
3. Validates that the post type is LiveBlog — returns an error for any other post type.
4. Fetches the update stream from `/post/{post_id}/live-blog-updates/`.
5. Returns `{"post": {...}, "updates": {...}}` combining both responses.

---

#### `fetch_trending_posts`
**API:** `GET /posts/trending/`

Returns posts ranked by page views over a configurable analytics window.

**Flow:**
1. Tool receives `duration` (`24h` / `7d` / `30d`), optional `type` filter, `page`, `limit`.
2. Calls `/posts/trending/?duration={duration}&...`.
3. Returns posts ordered by descending view count for the window.

> Requires Publive analytics tracking to be active on the publisher's site. Rankings refresh every 5 min; CDN adds another 5 min lag.

---

### Categories

#### `fetch_published_categories`
**API:** `GET /category/`

Returns all categories with their hierarchy.

**Flow:**
1. Tool receives optional `page` / `limit`.
2. Calls `/category/`.
3. Returns list with id, name, slug, parent_category, brand_color, absolute_url.

---

#### `fetch_published_category`
**API:** `GET /category/{identifier}/`

Returns full details of one category including SEO metadata, OG tags, and child categories.

**Flow:**
1. Tool receives `identifier` (ID or slug).
2. Calls `/category/{identifier}/`.
3. Returns id, name, slug, content, child_categories list, parent_category, meta_title, meta_description, og_*, twitter_*.

---

### Tags

#### `fetch_published_tags`
**API:** `GET /tag/`

Returns all tags, paginated.

**Flow:**
1. Tool receives optional `page` / `limit`.
2. Calls `/tag/`.
3. Returns list with id, name, slug, absolute_url.

---

#### `fetch_published_tag`
**API:** `GET /tag/{identifier}/`

Returns full details of one tag including SEO and social metadata.

**Flow:**
1. Tool receives `identifier` (ID or slug).
2. Calls `/tag/{identifier}/`.
3. Returns id, name, slug, display_name, meta_title, meta_description, og_*, twitter_*.

---

### Authors

#### `fetch_authors`
**API:** `GET /member/`

Returns all authors with profile info and social links.

**Flow:**
1. Tool receives optional `page` / `limit`.
2. Calls `/member/`.
3. Returns list with id, name, slug, email, description, avatar, linkedin, twitter, instagram, facebook.

---

#### `fetch_author`
**API:** `GET /member/{identifier}/`

Returns full profile of one author including SEO metadata.

**Flow:**
1. Tool receives `identifier` (numeric ID).
2. Calls `/member/{identifier}/`.
3. Returns full profile with SEO fields (meta_title, og_*, twitter_*) and absolute_url.

---

### Publisher / Site Config

#### `fetch_publisher_profile`
**API:** `GET /publisher-data/`

Returns the publisher profile: branding, social links, SEO defaults, and configuration.

**Flow:**
1. No input parameters.
2. Calls `/publisher-data/`.
3. Returns name, logo, favicon, primary/secondary colors, social_links, meta_title, meta_description.
4. Falls back to `/footer/` if the primary endpoint is unavailable.

---

#### `fetch_site_navigation`
**API:** `GET /navbar/`

Returns the navigation menu structure with nested hierarchy.

**Flow:**
1. No input parameters.
2. Calls `/navbar/`.
3. Returns list of menu items with `name`, `link`, `open_new_tab`, and nested `children` array.

---

#### `fetch_site_footer`
**API:** `GET /footer/`

Returns footer configuration: social links, quick menu, app links, branding, copyright.

**Flow:**
1. No input parameters.
2. Calls `/footer/`.
3. Returns logo, socialLinks, addQuickMenu, app_links (iOS/Android/Web), copyRightText, newsletter flag.

---

#### `fetch_newsletter_groups`
**API:** `GET /newsletter-groups/`

Returns all newsletter subscription groups.

**Flow:**
1. No input parameters.
2. Calls `/newsletter-groups/`.
3. Returns list with id, name, logo_url, description.

---

### Content Utilities

#### `resolve_url_to_content_type`
**API:** `GET /identify_url/?legacy_url={path}`

Resolves any incoming URL path to a content type, enabling dynamic routing on the frontend.

**Flow:**
1. Tool receives a `legacy_url` path (e.g. `/news/my-story`, `learn-banking`, `/tags/ipl`, `/author/jane`).
2. Calls `/identify_url/?legacy_url={path}`.
3. Returns `type` (one of: `post`, `category`, `tag`, `member`, `redirect`, `not_found`) plus:
   - `post` → full post object in `data.content`
   - `category` / `tag` / `member` → entity confirmed; call the details endpoint for full data
   - `redirect` → `data.url` is the destination
   - `not_found` → render 404

---

#### `fetch_ad_slots`
**API:** `GET /active-slots/`

Returns advertisement slot configurations.

**Flow:**
1. No input parameters.
2. Calls `/active-slots/`.
3. Returns list of slots with id, name, type (`CodeEditor`), dimensions/HTML content, and registered_slot metadata.

---

#### `fetch_content_type_definitions`
**API:** `GET /content-types/`

Returns all content types defined for the publisher (Article, Video, Web Story, etc.).

**Flow:**
1. No input parameters.
2. Calls `/content-types/`.
3. Returns list with name, api_slug, api_collections_slug.

---

#### `fetch_form_schema`
**API:** `GET /form-schema/{form_id}/`

Returns the field schema for a Publive form (for rendering form UI on the frontend).

**Flow:**
1. Tool receives `schema_id`.
2. Calls `/form-schema/{schema_id}/`.
3. Returns field definitions used to render and validate the form client-side.

---

### Sitemaps

#### `fetch_sitemap_index`
**API:** `GET /sitemap/allcontent-sitemap.xml/`

Returns the master sitemap index linking to all content sitemaps.

---

#### `fetch_sitemap_web_index`
**API:** `GET /sitemap/webcontent-sitemap.xml/`

Returns the web content sitemap index linking to paginated article sitemaps.

---

#### `fetch_sitemap_web_stories`
**API:** `GET /sitemap/webstory-sitemap.xml/`

Returns the web story sitemap index linking to paginated web story sitemaps.

---

#### `fetch_sitemap_news`
**API:** `GET /sitemap/news-sitemap.xml/`

Returns the Google News sitemap.

---

#### `fetch_sitemap_categories`
**API:** `GET /sitemap/category-sitemap.xml/`

Returns the sitemap listing all published category pages.

---

#### `fetch_sitemap_page`
**API:** `GET /sitemap/sitemap_{date}.xml/` or `GET /sitemap/webstory_sitemap_{date}.xml/`

Returns a specific paginated date-stamped sitemap.

**Flow:**
1. Tool receives `date` (e.g. `2026-05-01`) and optional `type` (`article` or `webstory`).
2. Constructs the path based on type — `sitemap_{date}.xml` for articles, `webstory_sitemap_{date}.xml` for web stories.
3. Returns the sitemap XML wrapped in the CDS JSON envelope.

> Discover valid date values from `fetch_sitemap_web_index` or `fetch_sitemap_web_stories` first.

---

### Static Files

#### `fetch_ads_txt`
**API:** `GET /static/ads.txt/`

Returns the publisher's ads.txt file.

---

#### `fetch_robots_txt`
**API:** `GET /static/robots.txt/`

Returns the publisher's robots.txt file.

---

#### `fetch_service_worker_js`
**API:** `GET /static/service-worker.js/`

Returns the push notification service worker JavaScript file.

---

#### `fetch_push_notification_html`
**API:** `GET /static/{filename}/`

Returns one of the three push notification permission UI HTML files.

**Flow:**
1. Tool receives `filename` — one of `izooto.html`, `helper-iframe.html`, `permission-dialog.html`.
2. Calls `/static/{filename}/`.
3. Returns file content wrapped in the CDS JSON envelope.

---

## Part 2 — Content Management Service (CMS) Tools

Write tools. Base URL: `https://cms.thepublive.com/publisher/{publisher_id}/`

CMS tools call `cms_client.cms_get/post/patch/delete(credentials, path, ...)` with a 10s timeout and no automatic retry.

**Safety tiers:**
| Operation | Default behavior |
|-----------|-----------------|
| GET (list/retrieve) | Executes directly, returns data |
| POST (create) | `dry_run=True` by default — previews what would be created without writing |
| PATCH (update) | `dry_run=True` by default — shows a human-readable diff of old vs new fields |
| DELETE | Requires both `dry_run=false` AND `confirm_delete=true` to execute |

> **Exception:** Draft posts (status `Draft`) skip dry_run and write immediately.

---

### Editorial Posts

#### `list_editorial_posts`
**API:** `GET /post/`

Lists all posts including drafts, scheduled, and published.

**Flow:** Calls `/post/?page=X&limit=Y`. Returns count, next/previous pagination URLs, and results array with full post metadata including `status`, `seo_score`, and `source`.

---

#### `get_editorial_post`
**API:** `GET /post/{id}/`

Retrieves a single post by integer ID (not slug — CMS uses numeric IDs only).

**Flow:** Calls `/post/{id}/`. Returns full post including `content` (HTML), `published_at`, `media_file_banner` object, `seo_keyphrase`, and `source: "HeadlessCMS"`.

---

#### `create_post`
**API:** `POST /post/`

Creates a new post.

**Flow:**
1. Tool receives title, english_title (immutable, used for slug), type (immutable), status, primary_category, and optional fields.
2. With `dry_run=True` (default): returns a preview of what would be created.
3. With `dry_run=False`: POSTs to `/post/` with JSON body.
4. Returns `201 Created` with the new post object including auto-generated id and slug.

**Immutable after creation:** `english_title`, `type`, `slug`, `meta_data.access_type`, `custom_published_at`.

---

#### `update_post`
**API:** `PATCH /post/{id}/`

Partially updates an existing post (only send changed fields).

**Flow:**
1. Tool receives `id` plus any updatable fields (title, content, status, categories, tags, contributors, banner_url, etc.).
2. With `dry_run=True` (default): fetches current state and shows diff preview.
3. With `dry_run=False`: PATCHes `/post/{id}/` with only the changed fields.
4. Returns updated post object.

> To publish a draft: send `{"status": "Published"}`.
> To schedule: send `{"status": "Scheduled", "scheduled_at": "2026-12-01T09:00:00Z"}`.

---

#### `delete_post`
**API:** `DELETE /post/{id}/`

Permanently deletes a post and all associated data. Cannot be undone.

**Flow:**
1. Tool receives `id`.
2. Requires `dry_run=false` AND `confirm_delete=true` to proceed.
3. DELETEs `/post/{id}/`.
4. Returns `{"status": "success", "message": "Deleted successfully!"}`.

---

### Editorial Categories

#### `list_editorial_categories`
**API:** `GET /category/`

Lists all categories (including drafts and inactive ones not visible on CDS).

**Flow:** Calls `/category/?page=X&limit=Y`. Returns count + results with id, name, english_name, slug, parent_category, content_type.

---

#### `get_editorial_category`
**API:** `GET /category/{id}/`

Retrieves a single category by integer ID.

---

#### `create_category`
**API:** `POST /category/`

Creates a new category.

**Flow:**
1. Requires `name` and `english_name` (english_name is immutable, used for slug).
2. With `dry_run=False`: POSTs to `/category/`.
3. Returns new category with auto-generated id and slug.

**Immutable after creation:** `english_name`, `slug`, `parent_category`, `content_type`.

---

#### `update_category`
**API:** `PATCH /category/{id}/`

Partially updates a category. Updatable fields: `name`, `meta_title`, `meta_description`, `content`, `category_brand_color`, `priority`.

---

#### `delete_category`
**API:** `DELETE /category/{id}/`

Permanently deletes a category. Posts assigned to it will need to be reassigned.

**Flow:** Requires `dry_run=false` AND `confirm_delete=true`.

---

### Editorial Tags

#### `list_editorial_tags`
**API:** `GET /tag/`

Lists all tags.

---

#### `get_editorial_tag`
**API:** `GET /tag/{id}/`

Retrieves a single tag by integer ID.

---

#### `create_tag`
**API:** `POST /tag/`

Creates a new tag. Requires `name` and `english_name`.

**Immutable after creation:** `english_name`, `slug`.

---

#### `update_tag`
**API:** `PATCH /tag/{id}/`

Updates a tag. Updatable fields: `name`, `meta_title`, `meta_description`, `content`.

---

#### `delete_tag`
**API:** `DELETE /tag/{id}/`

Permanently deletes a tag. Requires `dry_run=false` AND `confirm_delete=true`.

---

### Media Library

#### `list_media_assets`
**API:** `GET /media-library/`

Lists all media assets (images, videos, files).

**Flow:** Returns count + results with id, filename, alt_text, caption, path (CDN URL), source, type, meta_data (width/height), date, member.

---

#### `get_media_asset`
**API:** `GET /media-library/{id}/`

Retrieves a single media asset by integer ID.

---

#### `register_media_asset`
**API:** `POST /media-library/`

Registers an externally-hosted media URL into the CMS library.

**Flow:**
1. Requires `filename` and `path` (a direct CDN/S3/Cloudinary URL — not a file upload).
2. With `dry_run=False`: POSTs to `/media-library/` with JSON body.
3. Returns new media record with auto-generated id.

> This endpoint does NOT accept file uploads (`multipart/form-data`). The file must already be hosted externally.

**Immutable after creation:** `path`, `type`.

---

#### `update_media_asset`
**API:** `PATCH /media-library/{id}/`

Updates media metadata. Updatable: `filename`, `alt_text`, `caption`, `source`, `meta_data`.

---

#### `delete_media_asset`
**API:** `DELETE /media-library/{id}/`

Permanently deletes a media asset. Posts referencing it will lose their image. Requires `dry_run=false` AND `confirm_delete=true`.

---

### Live Blog Updates

These tools manage the individual update entries within a LiveBlog-type post.

#### `list_editorial_liveblog_updates`
**API:** `GET /post/{post_id}/live-blog-update/`

Lists all updates for a LiveBlog post.

---

#### `get_liveblog_update`
**API:** `GET /post/{post_id}/live-blog-update/{id}/`

Retrieves a single update entry.

---

#### `add_liveblog_update`
**API:** `POST /post/{post_id}/live-blog-update/`

Adds a new update entry to a LiveBlog post.

**Flow:**
1. Requires `post_id`, `title`, and `content` (HTML).
2. With `dry_run=False`: POSTs to `/post/{post_id}/live-blog-update/`.
3. Returns new update entry with id, author attribution, timestamps, `is_pinned: false`.

---

#### `update_liveblog_update`
**API:** `PATCH /post/{post_id}/live-blog-update/{id}/`

Updates the title/content of an existing live blog update entry.

---

#### `delete_liveblog_update`
**API:** `DELETE /post/{post_id}/live-blog-update/{id}/`

Permanently deletes a live blog update entry. Requires `dry_run=false` AND `confirm_delete=true`.

---

### Component Schemas

Custom components are reusable structured content blocks (e.g. hero banners, author bios) that can be embedded in posts via the `custom_entity` field.

#### `list_component_schemas`
**API:** `GET /component-schema/`

Lists all custom component schemas defined for the publisher.

---

#### `get_component_schema`
**API:** `GET /component-schema/{id}/`

Retrieves a single component schema.

---

#### `create_component_schema`
**API:** `POST /component-schema/`

Creates a new component schema definition.

**Flow:** Requires `name`. Creates the schema that posts can reference via `custom_entity.{field}.schema_slug`.

---

#### `update_component_schema`
**API:** `PATCH /component-schema/{id}/`

Updates a component schema's display name or field definitions.

---

#### `delete_component_schema`
**API:** `DELETE /component-schema/{id}/`

Permanently deletes a component schema. Requires `dry_run=false` AND `confirm_delete=true`.

---

### Content Type Schemas

Custom content types define new post formats beyond the built-in types (Article, Video, etc.).

#### `list_content_type_schemas`
**API:** `GET /custom-content-type/`

Lists all custom content types.

---

#### `get_content_type_schema`
**API:** `GET /custom-content-type/{id}/`

Retrieves a single custom content type.

---

#### `create_content_type_schema`
**API:** `POST /custom-content-type/`

Creates a new custom content type.

**Flow:** Requires `name`, `api_slug` (e.g. `movies`), and `api_collections_slug` (e.g. `movies-list`). These become the API endpoints for posts of this type.

---

#### `update_content_type_schema`
**API:** `PATCH /custom-content-type/{id}/`

Updates the display name of a custom content type.

---

#### `delete_content_type_schema`
**API:** `DELETE /custom-content-type/{id}/`

Permanently deletes a custom content type. Requires `dry_run=false` AND `confirm_delete=true`.

---

### Forms

#### `submit_form`
**API:** `POST /form/{form_id}/submit/` (CMS)

Submits a form response.

**Flow:**
1. Tool receives `form_schema_id` and field values.
2. POSTs directly to the form submission endpoint (no dry_run — submissions are always live).
3. Returns submission confirmation.

> Use `fetch_form_schema` (CDS) first to discover the form's required fields before submitting.

---

## Quick Reference Table

| Tool | Service | HTTP Method | Endpoint | Dry-run? |
|------|---------|-------------|----------|----------|
| `fetch_published_posts` | CDS | GET | `/posts/` | — |
| `fetch_published_post` | CDS | GET | `/posts/{id-or-slug}/` | — |
| `fetch_post_by_url` | CDS | GET | `/post/?legacy_url=` | — |
| `fetch_livebupdates` | CDS | GET | `/posts/{post_id}/live-blog-update/` | — |
| `fetch_liveblog_with_updates` | CDS | GET | `/post/{post_id}/` + `/post/{post_id}/live-blog-updates/` | — |
| `fetch_trending_posts` | CDS | GET | `/posts/trending/` | — |
| `fetch_published_categories` | CDS | GET | `/category/` | — |
| `fetch_published_category` | CDS | GET | `/category/{id-or-slug}/` | — |
| `fetch_published_tags` | CDS | GET | `/tag/` | — |
| `fetch_published_tag` | CDS | GET | `/tag/{id-or-slug}/` | — |
| `fetch_authors` | CDS | GET | `/member/` | — |
| `fetch_author` | CDS | GET | `/member/{id}/` | — |
| `fetch_publisher_profile` | CDS | GET | `/publisher-data/` | — |
| `fetch_site_navigation` | CDS | GET | `/navbar/` | — |
| `fetch_site_footer` | CDS | GET | `/footer/` | — |
| `fetch_newsletter_groups` | CDS | GET | `/newsletter-groups/` | — |
| `resolve_url_to_content_type` | CDS | GET | `/identify_url/?legacy_url=` | — |
| `fetch_ad_slots` | CDS | GET | `/active-slots/` | — |
| `fetch_content_type_definitions` | CDS | GET | `/content-types/` | — |
| `fetch_form_schema` | CDS | GET | `/form-schema/{id}/` | — |
| `fetch_sitemap_index` | CDS | GET | `/sitemap/allcontent-sitemap.xml/` | — |
| `fetch_sitemap_web_index` | CDS | GET | `/sitemap/webcontent-sitemap.xml/` | — |
| `fetch_sitemap_web_stories` | CDS | GET | `/sitemap/webstory-sitemap.xml/` | — |
| `fetch_sitemap_news` | CDS | GET | `/sitemap/news-sitemap.xml/` | — |
| `fetch_sitemap_categories` | CDS | GET | `/sitemap/category-sitemap.xml/` | — |
| `fetch_sitemap_page` | CDS | GET | `/sitemap/sitemap_{date}.xml/` | — |
| `fetch_ads_txt` | CDS | GET | `/static/ads.txt/` | — |
| `fetch_robots_txt` | CDS | GET | `/static/robots.txt/` | — |
| `fetch_service_worker_js` | CDS | GET | `/static/service-worker.js/` | — |
| `fetch_push_notification_html` | CDS | GET | `/static/{filename}/` | — |
| `list_editorial_posts` | CMS | GET | `/post/` | — |
| `get_editorial_post` | CMS | GET | `/post/{id}/` | — |
| `create_post` | CMS | POST | `/post/` | Yes (default on) |
| `update_post` | CMS | PATCH | `/post/{id}/` | Yes (default on) |
| `delete_post` | CMS | DELETE | `/post/{id}/` | Yes (must turn off) |
| `list_editorial_categories` | CMS | GET | `/category/` | — |
| `get_editorial_category` | CMS | GET | `/category/{id}/` | — |
| `create_category` | CMS | POST | `/category/` | Yes (default on) |
| `update_category` | CMS | PATCH | `/category/{id}/` | Yes (default on) |
| `delete_category` | CMS | DELETE | `/category/{id}/` | Yes (must turn off) |
| `list_editorial_tags` | CMS | GET | `/tag/` | — |
| `get_editorial_tag` | CMS | GET | `/tag/{id}/` | — |
| `create_tag` | CMS | POST | `/tag/` | Yes (default on) |
| `update_tag` | CMS | PATCH | `/tag/{id}/` | Yes (default on) |
| `delete_tag` | CMS | DELETE | `/tag/{id}/` | Yes (must turn off) |
| `list_media_assets` | CMS | GET | `/media-library/` | — |
| `get_media_asset` | CMS | GET | `/media-library/{id}/` | — |
| `register_media_asset` | CMS | POST | `/media-library/` | Yes (default on) |
| `update_media_asset` | CMS | PATCH | `/media-library/{id}/` | Yes (default on) |
| `delete_media_asset` | CMS | DELETE | `/media-library/{id}/` | Yes (must turn off) |
| `list_editorial_liveblog_updates` | CMS | GET | `/post/{post_id}/live-blog-update/` | — |
| `get_liveblog_update` | CMS | GET | `/post/{post_id}/live-blog-update/{id}/` | — |
| `add_liveblog_update` | CMS | POST | `/post/{post_id}/live-blog-update/` | Yes (default on) |
| `update_liveblog_update` | CMS | PATCH | `/post/{post_id}/live-blog-update/{id}/` | Yes (default on) |
| `delete_liveblog_update` | CMS | DELETE | `/post/{post_id}/live-blog-update/{id}/` | Yes (must turn off) |
| `list_component_schemas` | CMS | GET | `/component-schema/` | — |
| `get_component_schema` | CMS | GET | `/component-schema/{id}/` | — |
| `create_component_schema` | CMS | POST | `/component-schema/` | Yes (default on) |
| `update_component_schema` | CMS | PATCH | `/component-schema/{id}/` | Yes (default on) |
| `delete_component_schema` | CMS | DELETE | `/component-schema/{id}/` | Yes (must turn off) |
| `list_content_type_schemas` | CMS | GET | `/custom-content-type/` | — |
| `get_content_type_schema` | CMS | GET | `/custom-content-type/{id}/` | — |
| `create_content_type_schema` | CMS | POST | `/custom-content-type/` | Yes (default on) |
| `update_content_type_schema` | CMS | PATCH | `/custom-content-type/{id}/` | Yes (default on) |
| `delete_content_type_schema` | CMS | DELETE | `/custom-content-type/{id}/` | Yes (must turn off) |
| `validate_media_asset` | CMS | GET | `/media-library/{id}/` | — |
| `validate_category` | CMS | GET | `/category/{id}/` | — |
| `validate_author` | CDS | GET | `/author/{id}/` | — |
| `validate_post_slug` | CMS | GET | `/post/{slug}/` | — |
| `submit_form` | CMS | POST | `/form/{id}/submit/` | No |

---

## Part 3 — Test Questions per Tool

For each tool, questions are grouped into: **Happy Path**, **Edge Cases**, and **Error / Rejection Cases**.
Ask these questions against a live MCP session to confirm every code path is exercised.

---

### CDS 

**Happy Path**
1. "List the first 10 published posts."
2. "Show me all Articles — give me the first page."
3. "List posts in category ID 100, limit 5."
4. "Find posts whose title contains 'budget'."
5. "Get posts by author ID 1."
6. "List posts tagged with tag ID 500."
7. "Show me Video posts only."
8. "List posts published after 2026-01-01."
9. "Get page 2 of all posts, 20 per page."
10. "Show me posts that are Articles AND in category 100 at the same time."
2. "Get post with slug 'union-budget-2026-highlights'."
3. "Fetch a post and confirm it has content_html, categories, tags, and contributors in the response."
4. "Fetch a post and check that media_file_banner includes width and height in meta_data."
1. "Fetch post at legacy URL '/business/union-budget-2026-12345'."
2. "Resolve the URL '/news/breaking-story-98765' — does it return the full post object?"


**Edge Cases**
11. "List posts with limit set to the maximum (51)." - Error
12. "Request page 1000 — what comes back?" Page 1000 works! The API handles it gracefully, returning 10 posts from deep in the archive — all dated around February 1, 2025.
13. "Filter by multiple types at once: Articles and Videos." - Not Error but different solutions
14. "Filter posts NOT of type Gallery."
15. "Search for posts with word_count greater than 1000."
16. "List posts with created_at between 2026-01-01 and 2026-03-31."
17. "Request page 1 with limit 1 — returns exactly one result?"
18. "Use title__contains with a single character string."
5. "Fetch a post by its numeric ID passed as a string — does it still work?"
6. "Fetch the same post twice — are the responses identical?"
3. "Pass a URL with query string characters in it — is it handled cleanly?"
4. "Pass a URL with a trailing slash vs without — do both work?"

**Error / Rejection Cases**
19. "Call list_posts with an invalid type value like 'BlogPost' — does it error?"
20. "Request page 0 — is that rejected?"
21. "Set limit to 100 (above max) — does the API cap it or reject?"
22. "Use an unknown filter key like 'foo__eq=bar' — what happens?"
7. "Get post with ID 9999999 (non-existent) — expect 'Post not found'."
8. "Get post with slug 'this-slug-does-not-exist' — expect error."
9. "Call with an empty string as identifier — what happens?"
5. "Pass legacy_url='/does-not-exist' — expect 'Post not found'."
6. "Call with no legacy_url parameter at all — what error is returned?"



**Happy Path**
1. "Get the first page of live blog updates for LiveBlog post ID X."
2. "Get page 2 of updates, 5 per page."
3. "Confirm each update has: id, author, title, content, is_pinned, created_at."
4. "Check that the pinned update (is_pinned: true) appears in the results."
1. "Fetch the LiveBlog post details AND its updates combined for post ID X."
2. "Confirm the response has both a 'post' object and an 'updates' list."
1. "Show me trending posts for the last 24 hours."
2. "Get trending posts for the last 7 days."
3. "Get trending posts for the last 30 days."
4. "Get trending Articles only for the past 24 hours."
5. "Get top 5 trending posts."
6. "Confirm the first result has the highest view count in the list."
1. "List all categories."
2. "Get page 2 of categories, 5 per page."
3. "Confirm each category has: id, name, slug, absolute_url."
4. "Check that at least one category has a non-null parent_category (i.e., a sub-category exists)."
3. "Confirm the response includes child_categories, meta_title, og_title."
4. "Fetch a top-level category and verify parent_category is null."
5. "Fetch a child category and verify parent_category is populated."
3. "Confirm response has meta_title, og_title, twitter_title."
3. "Confirm each author has: id, name, slug, avatar, email."
4. "Check that at least one author has social links (twitter or linkedin)."
2. "Get page 1, 10 per page — confirm pagination fields are present."
3. "Confirm each tag has id, name, slug, absolute_url."
2. "Get author with slug 'jane-doe'."
3. "Confirm response has meta_title, og_image, absolute_url."
1. "Get the publisher profile data."
2. "Confirm the response has name, logo, favicon, social_links."
3. "Confirm meta_title and meta_description are present."
1. "Get the navigation menu."
2. "Confirm at least one menu item has nested children."
3. "Check that each item has name, link, open_new_tab."
1. "Get the footer config."
2. "Confirm socialLinks, addQuickMenu, and copyRightText are present."
3. "Check that app_links contains ios and android URLs."
1. "Get all newsletter groups."
2. "Confirm each group has id, name, description, logo_url."
1. "Identify what content lives at '/news/some-article-12345' — expect type: 'post' with full post data."
2. "Identify 'news' (a category slug) — expect type: 'category'."
3. "Identify '/tags/ipl-2026' — expect type: 'tag'."
4. "Identify '/author/jane-doe' — expect type: 'member'."
5. "Identify a path that has a redirect rule — expect type: 'redirect' with destination URL."
6. "Identify '/this-path-does-not-exist' — expect type: 'not_found'."
1. "Get all active advertisement slots."
2. "Confirm each slot has id, name, type, and a data object with html or size."
1. "Get all content types."
2. "Confirm at least 'Article' and 'Video' are in the list."
3. "Confirm each type has name, api_slug, api_collections_slug."



**Edge Cases**
4. "What happens if the navbar has no nested items — is the children field null or an empty array?"
3. "What is returned if the publisher has no newsletter groups configured — empty list?"
7. "Pass a root path '/' — what type comes back?"
8. "Pass a path with uppercase letters — is it case-insensitive?"
9. "When type is 'post', confirm data.content contains a full post payload."
10. "When type is 'redirect', confirm data.url is a valid destination."
3. "What is returned if no ad slots are configured — empty list?"
4. "Call it twice back to back — do both calls return the same data?"
4. "List tags with limit=1 — single tag returned?"
5. "Request a very high page number beyond total count — empty list returned?"
7. "Request trending posts with limit=1 — single top post returned?"
8. "Request page 2 of trending posts."
9. "Filter trending by type 'Web Story'."
10. "Filter trending by type 'LiveBlog'."
5. "Request updates for a LiveBlog post with zero updates — does it return an empty list?"
6. "Set limit to 50 (max) — do all updates come back on one page?"




**Error / Rejection Cases**
11. "Call without a legacy_url param — expect a validation error."
4. "Get author with ID 9999999 — expect error."
5. "Get author with slug 'nobody-here' — expect error."
6. "Get category with ID 9999999 — expect 'Not found'."
7. "Get category with slug 'nonexistent-slug' — expect error."
6. "Call with limit=0 — is it rejected or treated as default?"
4. "Get tag with ID 9999999 — expect 'Not found'."
5. "Get tag with slug 'no-such-tag' — expect error."
11. "Call with duration='48h' (invalid value) — expect validation error."
12. "Call with duration='1d' (invalid) — expect error."
13. "Call with an invalid type value like 'Podcast' — expect error."
3. "Call with an Article post ID — expect a type mismatch error."
4. "Call with a non-existent ID — expect 'Post not found'."
7. "Call with a post_id that belongs to an Article (not a LiveBlog) — what does the API return?"
8. "Call with a non-existent post_id — expect 'Post not found'."





---

### CMS — `list_editorial_posts` / `cms_list_posts`

**Happy Path**
1. "List all CMS posts."
2. "Confirm the response has count, next, previous, and results."
3. "Check that results include posts with status 'Draft' (not just Published)."
4. "Confirm each result has seo_score and source fields."
5. "Get page 2, limit 5."

---

### CMS — `get_editorial_post` / `cms_get_post`

**Happy Path**
1. "Get CMS post with ID 50123."
2. "Confirm the response has content (HTML), published_at, media_file_banner."
3. "Confirm source is 'HeadlessCMS'."

**Error / Rejection Cases**
4. "Get post with ID 9999999 — expect 'Not found'."
5. "Try to get a post using its slug (CMS only accepts numeric IDs) — what happens?"

---

### CMS — `create_post` / `cms_create_post`

**Happy Path (dry_run)**
1. "Preview creating a new Article titled 'Test Article' with primary_category 100 and status Draft — use dry_run."
2. "Preview creating a Published Video post — confirm the preview shows what would be created."
3. "Preview creating a Scheduled post with scheduled_at set to a future date."

**Happy Path (live)**
4. "Create a new Draft Article titled 'API Test Post' with english_title 'API Test Post', primary_category 100. Set dry_run=false."
5. "Confirm the returned post has a newly assigned id and auto-generated slug."
6. "Create a post with tags, categories, contributors, and short_description all populated."
7. "Create a post with custom_published_at set to a past date (backdated publish)."

**Edge Cases**
8. "Create a post with the minimum required fields only (title, english_title, type, status, primary_category)."
9. "Create a post where english_title differs from title (localized title)."
10. "Create a post with status 'Approval Pending'."
11. "Create a post with hide_banner_image: true — confirm the field is saved."

**Error / Rejection Cases**
12. "Try to create a post without the required 'type' field — expect validation error."
13. "Try to create a post with type='Podcast' (invalid) — expect error."
14. "Try to create a Scheduled post without scheduled_at — expect error."
15. "Create a post and then try to update its english_title — expect immutability error."
16. "Create a post and then try to update its type — expect immutability error."

---

### CMS — `update_post` / `cms_update_post`

**Happy Path (dry_run)**
1. "Preview changing post 50123's status from Draft to Published — use dry_run, confirm diff shows old vs new."
2. "Preview updating the title of post 50123 — confirm diff output."
3. "Preview adding a new tag to post 50123 — does diff show the tag addition?"

**Happy Path (live)**
4. "Publish post 50123 by setting status to 'Published'. Set dry_run=false."
5. "Update the title of post 50123 to 'Updated Title'. Set dry_run=false."
6. "Change primary_category of post 50123. Set dry_run=false."
7. "Set hide_banner_image to true on post 50123. Set dry_run=false."
8. "Schedule post 50123 by setting status='Scheduled' and scheduled_at to a future timestamp."

**Edge Cases**
9. "Send a PATCH with no fields changed — what does the API return?"
10. "Update only the short_description field, leave everything else untouched."

**Error / Rejection Cases**
11. "Update post with ID 9999999 (non-existent) — expect 'Not found'."
12. "Try to update the slug of an existing post — expect immutability error."

---

### CMS — `delete_post` / `cms_delete_post`

**Happy Path**
1. "Delete post ID X with dry_run=false and confirm_delete=true."
2. "After deletion, try to get the same post ID — expect 'Not found'."

**Edge Cases**
3. "Call delete with only dry_run=false but confirm_delete not set — should it block?"
4. "Call delete with only confirm_delete=true but dry_run still true — should it block?"

**Error / Rejection Cases**
5. "Delete post with ID 9999999 — expect 'Not found'."
6. "Call delete without setting confirm_delete=true — expect the safety guard to block it."

---

### CMS — `list_editorial_categories` / `cms_list_categories`

**Happy Path**
1. "List all CMS categories."
2. "Confirm results include english_name, slug, content_type fields."
3. "Confirm count matches the total number of categories."

---

### CMS — `get_editorial_category` / `cms_get_category`

**Happy Path**
1. "Get CMS category with ID 14428."
2. "Confirm it returns english_name and slug."

**Error / Rejection Cases**
3. "Get category with ID 9999999 — expect 'Not found'."

---

### CMS — `create_category` / `cms_create_category`

**Happy Path (dry_run)**
1. "Preview creating a category named 'Sports' with english_name 'Sports' — use dry_run."

**Happy Path (live)**
2. "Create category 'Technology' with english_name 'Technology' and meta_title 'Tech News'. dry_run=false."
3. "Create a child category with parent_category set to an existing category ID."
4. "Create a category with category_brand_color '#FF0000'."

**Error / Rejection Cases**
5. "Create a category without english_name — expect validation error."
6. "After creating, try to update the english_name — expect immutability error."
7. "After creating, try to update the slug — expect immutability error."
8. "Try to create two categories with the same english_name — does it reject or create a duplicate?"

---

### CMS — `update_category` / `cms_update_category`

**Happy Path**
1. "Update the name of category 14428 to 'Latest News'. dry_run=false."
2. "Update meta_description of a category. dry_run=false."
3. "Change the brand color of a category."
4. "Preview the update with dry_run=true first, then execute with dry_run=false."

**Error / Rejection Cases**
5. "Update category 9999999 — expect 'Not found'."

---

### CMS — `delete_category` / `cms_delete_category`

**Happy Path**
1. "Delete category ID X with dry_run=false and confirm_delete=true."
2. "After deletion, try to get the same category — expect 'Not found'."

**Error / Rejection Cases**
3. "Delete without confirm_delete=true — expect safety guard block."
4. "Delete category 9999999 — expect 'Not found'."

---

### CMS — `list_editorial_tags` / `cms_list_tags`

**Happy Path**
1. "List all CMS tags."
2. "Confirm results contain english_name and meta_title fields."

---

### CMS — `get_editorial_tag` / `cms_get_tag`

**Happy Path**
1. "Get tag with ID 500."
2. "Confirm it returns english_name, slug, meta_title."

**Error / Rejection Cases**
3. "Get tag 9999999 — expect 'Not found'."

---

### CMS — `create_tag` / `cms_create_tag`

**Happy Path (dry_run)**
1. "Preview creating tag 'AI Technology' with english_name 'AI Technology'."

**Happy Path (live)**
2. "Create tag 'AI Technology' with english_name 'AI Technology'. dry_run=false."
3. "Create a tag with meta_title and meta_description set."
4. "Create a tag with an HTML content description."

**Error / Rejection Cases**
5. "Create tag without english_name — expect validation error."
6. "After creating, try to update the english_name — expect immutability error."

---

### CMS — `update_tag` / `cms_update_tag`

**Happy Path**
1. "Update the name of tag 500 to 'Breaking News Updated'. dry_run=false."
2. "Update meta_description of tag 500."

**Error / Rejection Cases**
3. "Update tag 9999999 — expect 'Not found'."

---

### CMS — `delete_tag` / `cms_delete_tag`

**Happy Path**
1. "Delete tag ID X with dry_run=false and confirm_delete=true."

**Error / Rejection Cases**
2. "Delete without confirm_delete=true — expect block."
3. "Delete tag 9999999 — expect 'Not found'."

---

### CMS — `list_media_assets` / `cms_list_media`

**Happy Path**
1. "List all media assets."
2. "Confirm each result has id, filename, path, type, alt_text, date."
3. "Get page 2, limit 10."

---

### CMS — `get_media_asset` / `cms_get_media`

**Happy Path**
1. "Get media asset with ID 999."
2. "Confirm it returns path (CDN URL), type (Image/Video/File), source, meta_data."

**Error / Rejection Cases**
3. "Get media asset 9999999 — expect 'Not found'."

---

### CMS — `register_media_asset` / `cms_create_media`

**Happy Path (dry_run)**
1. "Preview registering a media URL 'https://cdn.example.com/photo.jpg' with filename 'photo.jpg'."

**Happy Path (live)**
2. "Register media with filename 'hero.jpg', path 'https://cdn.example.com/hero.jpg', alt_text 'Hero Image'. dry_run=false."
3. "Register a Video type media asset."
4. "Register media with source 'Reuters' and a caption."
5. "Register media and then use its returned ID as banner_url in cms_create_post."

**Edge Cases**
6. "Register the same URL twice — does it create a duplicate or reject?"
7. "Register media without alt_text — is it optional?"

**Error / Rejection Cases**
8. "Attempt to register media without a path — expect validation error."
9. "Attempt to send a multipart/form-data file upload instead of a JSON URL — what error is returned?"
10. "After registering, try to update the path field — expect immutability error."
11. "After registering, try to update the type field — expect immutability error."

---

### CMS — `update_media_asset` / `cms_update_media`

**Happy Path**
1. "Update alt_text of media 999 to 'Updated Alt Text'. dry_run=false."
2. "Update source of media 999 to 'AFP'."
3. "Update both caption and source in a single PATCH."

**Error / Rejection Cases**
4. "Update media 9999999 — expect 'Not found'."

---

### CMS — `delete_media_asset` / `cms_delete_media`

**Happy Path**
1. "Delete media asset ID X with dry_run=false and confirm_delete=true."
2. "After deletion, confirm that any post referencing this media now has a null banner."

**Error / Rejection Cases**
3. "Delete without confirm_delete=true — expect block."
4. "Delete media 9999999 — expect 'Not found'."

---

### CMS — `list_editorial_liveblog_updates` / `cms_list_live_blog_updates`

**Happy Path**
1. "List all live blog updates for LiveBlog post ID X."
2. "Confirm each update has id, author, content (title + HTML), is_pinned, timestamps."

**Error / Rejection Cases**
3. "Call with a non-LiveBlog post ID — what is returned?"

---

### CMS — `get_liveblog_update` / `cms_get_live_blog_update`

**Happy Path**
1. "Get live blog update ID 1001 from post X."
2. "Confirm it has author attribution and is_pinned field."

**Error / Rejection Cases**
3. "Get update 9999999 from a valid post — expect 'Not found'."

---

### CMS — `add_liveblog_update` / `cms_create_live_blog_update`

**Happy Path (dry_run)**
1. "Preview adding a live blog update titled 'Breaking: New update' to post X."

**Happy Path (live)**
2. "Add a live blog update with title 'Live: Minister speaks' and HTML content to post X. dry_run=false."
3. "Confirm the returned update has is_pinned: false and correct author."
4. "Add multiple updates to the same post in sequence — confirm ordering."

**Error / Rejection Cases**
5. "Add an update to an Article post ID (not a LiveBlog) — expect error."
6. "Add an update without a title — expect validation error."
7. "Add an update without content — expect validation error."

---

### CMS — `update_liveblog_update` / `cms_update_live_blog_update`

**Happy Path**
1. "Update the title of live blog update 1001 in post X. dry_run=false."
2. "Update the content HTML of an update."
3. "Set is_pinned to true on an update."

**Error / Rejection Cases**
4. "Update a non-existent update ID — expect 'Not found'."

---

### CMS — `delete_liveblog_update` / `cms_delete_live_blog_update`

**Happy Path**
1. "Delete live blog update ID X from post Y with dry_run=false and confirm_delete=true."
2. "After deletion, list updates for the post — confirm the entry is gone."

**Error / Rejection Cases**
3. "Delete without confirm_delete=true — expect block."

---

### CMS — `list_component_schemas` / `cms_list_custom_components`

**Happy Path**
1. "List all custom component schemas."
2. "Confirm each schema has id, name, and field definitions."

---

### CMS — `get_component_schema` / `cms_get_custom_component`

**Happy Path**
1. "Get component schema with ID 1."
2. "Confirm it lists the fields that posts can use via custom_entity."

**Error / Rejection Cases**
3. "Get component schema 9999999 — expect 'Not found'."

---

### CMS — `create_component_schema` / `cms_create_custom_component`

**Happy Path**
1. "Preview creating a component schema named 'Author Bio'. dry_run=true."
2. "Create component schema 'Hero Banner' with a heading and cta_url field. dry_run=false."
3. "Confirm that after creation, the schema appears in list_component_schemas."

**Error / Rejection Cases**
4. "Create a component schema without a name — expect validation error."

---

### CMS — `update_component_schema` / `cms_update_custom_component`

**Happy Path**
1. "Rename component schema ID 1 to 'Author Profile'. dry_run=false."

---

### CMS — `delete_component_schema` / `cms_delete_custom_component`

**Happy Path**
1. "Delete component schema ID X with dry_run=false and confirm_delete=true."

**Error / Rejection Cases**
2. "Delete without confirm_delete=true — expect block."

---

### CMS — `list_content_type_schemas` / `cms_list_custom_content_types`

**Happy Path**
1. "List all custom content types."
2. "Confirm each has name, api_slug, api_collections_slug."

---

### CMS — `get_content_type_schema` / `cms_get_custom_content_type`

**Happy Path**
1. "Get custom content type with ID 1."
2. "Confirm api_slug and api_collections_slug are present."

**Error / Rejection Cases**
3. "Get custom content type 9999999 — expect 'Not found'."

---

### CMS — `create_content_type_schema` / `cms_create_custom_content_type`

**Happy Path**
1. "Preview creating a custom content type 'Movies' with api_slug 'movie' and api_collections_slug 'movies'."
2. "Create it with dry_run=false — confirm it appears in the list."

**Error / Rejection Cases**
3. "Create without api_slug — expect validation error."
4. "Create without api_collections_slug — expect validation error."
5. "Create with a duplicate api_slug — expect conflict error."

---

### CMS — `update_content_type_schema` / `cms_update_custom_content_type`

**Happy Path**
1. "Rename custom content type ID 1 display name to 'Films'. dry_run=false."

---

### CMS — `delete_content_type_schema` / `cms_delete_custom_content_type`

**Happy Path**
1. "Delete custom content type ID X with dry_run=false and confirm_delete=true."

**Error / Rejection Cases**
2. "Delete without confirm_delete=true — expect block."

---

### CMS — `submit_form`

**Happy Path**
1. "First call get_form_schema for form ID 1 to see required fields, then submit the form with all required values."
2. "Submit a form with all optional fields populated."
3. "Confirm the response returns a submission confirmation."

**Edge Cases**
4. "Submit the same form twice — are duplicate submissions allowed?"

**Error / Rejection Cases**
5. "Submit without a required field — expect validation error listing the missing field."
6. "Submit to a non-existent form_schema_id — expect error."
7. "Submit form data where a field value fails its type constraint (e.g., text in a number field) — expect error."

---

### Cross-Tool Flow Tests

These multi-step sequences test real-world workflows end to end.

1. **Publish a new article:**
   - `cms_create_post` (dry_run=true) → review preview → `cms_create_post` (dry_run=false) → `cms_update_post` status=Published → `fetch_published_post` to confirm it's live.

2. **Add a banner image to a post:**
   - `cms_create_media` with an external URL → note returned ID → `cms_update_post` with banner_url = that ID.

3. **Live blog event coverage:**
   - `cms_create_post` type=LiveBlog → `cms_create_live_blog_update` × 3 → `fetch_livebupdates` to read them back via CDS.

4. **Category and post together:**
   - `cms_create_category` → `cms_create_post` with primary_category = new category ID → `fetch_published_posts` filtered by that category ID.

5. **URL routing simulation:**
   - `resolve_url_to_content_type` for a post URL → confirm type=post → `fetch_published_post` by ID from data.content.

6. **Form submission flow:**
   - `fetch_form_schema` for form X → `submit_form` with values derived from the schema.

7. **Safe delete with dry_run:**
   - `cms_delete_post` with defaults (dry_run blocked) → confirm it is prevented → set dry_run=false AND confirm_delete=true → confirm deletion → `cms_get_post` returns 'Not found'.

8. **Trending to detail:**
   - `fetch_trending_posts` duration=7d → take the top result ID → `fetch_published_post` with that ID → compare title and slug.

