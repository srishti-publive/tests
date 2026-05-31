Categories (5 tools)

GET — direct result
1. "Show me all my CMS categories" → cms_list_categories
2. "Get me details for category ID 12" → cms_get_category

POST — confirm then create

3. "Create a new category called 'Sports' with english name 'sports'" → cms_create_category (should show dry run preview, ask to confirm)
4. "Create a subcategory called 'Cricket' under parent category 5" → tests parent_category + immutable field warning

PATCH — dry run then execute

5. "Update category 12 — change the name to 'Tech & Gadgets'" → cms_update_category (should show diff first)
6. "Change the brand color of category 8 to #FF5733" → tests partial update

DELETE — dry run then confirm

7. "Delete category ID 20" → cms_delete_category (should show preview, warn about posts losing category, ask to confirm)

---
Tags (5 tools)

GET
8. "List all my CMS tags" → cms_list_tags
9. "Get tag with ID 7" → cms_get_tag

POST
10. "Create a tag called 'Startup' with english name 'startup'" → cms_create_tag (dry run preview)
11. "Create a tag with SEO title and meta description" → tests optional fields collection

PATCH
12. "Update tag 7 — change the name to 'Startups & Funding'" → dry run diff first
13. "Update the meta description of tag 15" → partial patch

DELETE
14. "Delete tag ID 7" → preview, then confirm

---
Posts (5 tools)

GET
15. "List all posts in my CMS including drafts" → cms_list_posts
16. "Get post with ID 101" → cms_get_post

POST — Draft (no dry run)
17. "Create a draft post titled 'Breaking News' with english title 'breaking-news', type Article, primary category 3" → cms_create_post status=Draft → should create immediately, no confirmation

POST — Published (dry run required)
18. "Create and publish a new Article titled 'Top 10 Tips' in category 5" → status=Published → should show preview, ask to confirm

POST — missing info (should ask before calling)
19. "Create a new post about cricket" → missing title, english_title, type, status, primary_category → should ask for missing fields before calling any tool

PATCH — Draft (no dry run)
20. "Save post 101 as a draft" → cms_update_post status=Draft → should update immediately

PATCH — other updates (dry run first)
21. "Update the title of post 101 to 'New Title'" → should show diff first
22. "Publish post 101" → status=Published → dry run diff + requires confirm_publish=true
23. "Schedule post 101 for tomorrow" → status=Scheduled + scheduled_at → dry run

DELETE
24. "Delete post 101" → preview full post details, warn about permanent removal, ask to confirm

---
Live Blog Updates (5 tools)

GET
25. "List all updates for live blog post 55" → cms_list_live_blog_updates
26. "Get live blog update entry ID 9 from post 55" → cms_get_live_blog_update

POST
27. "Add an update to live blog 55: title 'Goal Scored!', content 'Team A scores in the 45th minute'" → dry run preview, confirm

POST — missing info
28. "Add an update to my live blog" → missing post_id, title, content → should ask for all three before calling

PATCH
29. "Edit live blog update 9 in post 55 — change the title to 'Red Card Issued'" → dry run diff first

DELETE
30. "Delete live blog update 9 from post 55" → preview, then confirm

---
Custom Components (5 tools)

GET
31. "List all my custom components" → cms_list_custom_components
32. "Get custom component with ID 3" → cms_get_custom_component

POST
33. "Create a custom component called 'Subscribe Banner' with HTML content <div>Subscribe Now</div>" → preview, confirm

PATCH
34. "Update the content of custom component 3 with new HTML" → dry run diff first

DELETE
35. "Delete custom component 3" → preview, confirm

---
Media Library (5 tools)

GET
36. "List all media in my library" → cms_list_media
37. "Get media asset ID 88" → cms_get_media

POST
38. "Register this image URL into the media library: filename hero.jpg, path https://s3.example.com/hero.jpg" → preview, confirm
39. "Add a media asset" → missing filename and path → should ask for required fields first

PATCH
40. "Update the alt text of media asset 88 to 'A cricket stadium at night'" → dry run diff first
41. "Change the caption of media 88" → partial patch, dry run

DELETE
42. "Delete media asset 88" → preview with warning (posts referencing it will break), confirm

---
Validation Tools (4 tools — read-only, direct result)

43. "Check if media ID 88 exists" → validate_media_exists
44. "Does category 12 exist?" → validate_category_exists
45. "Is author ID 5 valid?" → validate_author_exists
46. "Is the slug 'my-new-article' available?" → validate_post_slug

---
Edge Case / Workflow Tests

47. Confirm step rejection: During a POST dry run preview, say "No, change the name" — should update fields and show a new preview, not create
48. Delete cancellation: During a DELETE dry run, say "Cancel" — should stop, not proceed
49. Immutable field attempt: "Update the slug of category 12" — should warn the field is immutable
50. CDS vs CMS overlap: "Show me my categories" → should use CDS list_categories (simpler), and suggest CMS only if drafts/management needed










├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│        Suspect        │                                 Reason                                 │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ contributors silently │ Beta API may require at least one author ID despite docs saying        │
│  required             │ optional                                                               │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ english_title slug    │ Another post with same slug exists somewhere, API returns integer      │
│ conflict              │ error (odd but seen in DRF custom validators)                          │
├───────────────────────┼────────────────────────────────────────────────────────────────────────┤
│ Beta API bug          │ The /post/ endpoint on cms-beta may have a broken serializer field —   │
│                       │ category endpoint works because it's a different serializer            │
└───────────────────────┴───────────────────────────────────────────────────




