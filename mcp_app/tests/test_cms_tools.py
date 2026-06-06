"""Unit tests for CMS write tool handlers.

All tests mock cms_get / cms_post / cms_patch / cms_delete so no real HTTP
calls are made. Tests verify:
  - dry_run=True (default) returns a preview, makes no write call
  - dry_run=False executes the real write call
  - delete requires BOTH dry_run=false AND confirm_delete=true
  - publish requires confirm_publish=true
  - missing required fields return structured errors, not exceptions
"""
from unittest.mock import patch

from django.test import TestCase

CREDS = {"publisherId": "pub1", "apiKey": "key", "apiSecret": "secret"}
POST_DATA = {"id": 99, "title": "Old Title", "status": "Draft"}
OK_RESPONSE = {"data": POST_DATA}


# ── Posts ──────────────────────────────────────────────────────────────────────

class ListEditorialPostsTests(TestCase):
    @patch("mcp_app.cms.posts.cms_get", return_value=OK_RESPONSE)
    def test_list_passes_pagination(self, mock_get):
        from mcp_app.cms.posts import list_editorial_posts
        result = list_editorial_posts(CREDS, {"page": 2, "limit": 5})
        mock_get.assert_called_once_with(CREDS, "/post/", {"page": 2, "limit": 5})
        self.assertEqual(result, OK_RESPONSE)


class GetEditorialPostTests(TestCase):
    @patch("mcp_app.cms.posts.cms_get", return_value=POST_DATA)
    def test_fetches_by_id(self, mock_get):
        from mcp_app.cms.posts import get_editorial_post
        get_editorial_post(CREDS, {"id": 99})
        mock_get.assert_called_once_with(CREDS, "/post/99/")


class CreatePostTests(TestCase):
    BASE_ARGS = {
        "title": "Test Post",
        "english_title": "Test Post",
        "type": "Article",
        "status": "Draft",
        "primary_category": 1,
        "contributors": "12",
    }

    def _call(self, extra=None):
        from mcp_app.cms.posts import create_post
        args = {**self.BASE_ARGS, **(extra or {})}
        return create_post(CREDS, args)

    def test_missing_contributors_returns_error(self):
        from mcp_app.cms.posts import create_post
        args = {k: v for k, v in self.BASE_ARGS.items() if k != "contributors"}
        result = create_post(CREDS, args)
        self.assertEqual(result["error_type"], "missing_required_field")

    @patch("mcp_app.cms.posts.cms_post", return_value={"data": {"id": 5, "status": "Draft"}})
    def test_draft_status_creates_immediately(self, mock_post):
        result = self._call()
        mock_post.assert_called_once()
        # No dry_run key expected for Draft
        self.assertNotIn("dry_run", result)

    def test_non_draft_with_dry_run_true_returns_preview(self):
        result = self._call({"status": "Published", "dry_run": True})
        self.assertTrue(result.get("dry_run"))
        self.assertIn("preview", result)

    @patch("mcp_app.cms.posts.cms_post", return_value={"data": {"id": 7, "status": "Draft"}})
    @patch("mcp_app.cms.posts.cms_patch", return_value={"data": {"id": 7, "status": "Published"}})
    def test_published_with_dry_run_false_calls_post_then_patch(self, mock_patch, mock_post):
        _ = self._call({"status": "Published", "dry_run": False})
        mock_post.assert_called_once()
        mock_patch.assert_called_once()

    def test_video_type_returns_unsupported_error(self):
        result = self._call({"type": "Video"})
        self.assertEqual(result["error_type"], "unsupported_operation")

    def test_web_story_without_content_returns_error(self):
        result = self._call({"type": "Web Story"})
        self.assertEqual(result["error_type"], "missing_required_field")


class UpdatePostTests(TestCase):
    def _call(self, args):
        from mcp_app.cms.posts import update_post
        return update_post(CREDS, args)

    @patch("mcp_app.cms.posts.cms_get", return_value=POST_DATA)
    def test_dry_run_default_returns_diff_preview(self, mock_get):
        result = self._call({"id": 99, "title": "New Title"})
        self.assertTrue(result.get("dry_run"))
        self.assertIn("preview", result)
        mock_get.assert_called_once_with(CREDS, "/post/99/")

    @patch("mcp_app.cms.posts.cms_patch", return_value={"data": {"id": 99}})
    def test_draft_update_skips_dry_run(self, mock_patch):
        _ = self._call({"id": 99, "status": "Draft"})
        mock_patch.assert_called_once_with(CREDS, "/post/99/", {"status": "Draft"})

    def test_publish_without_confirm_returns_error(self):
        result = self._call({"id": 99, "status": "Published", "dry_run": False})
        self.assertEqual(result["error_type"], "confirmation_required")

    @patch("mcp_app.cms.posts.cms_patch", return_value={"data": {"id": 99, "status": "Published"}})
    def test_publish_with_confirm_publishes(self, mock_patch):
        _ = self._call({"id": 99, "status": "Published", "dry_run": False, "confirm_publish": True})
        mock_patch.assert_called_once()


class DeletePostTests(TestCase):
    def _call(self, args):
        from mcp_app.cms.posts import delete_post
        return delete_post(CREDS, args)

    @patch("mcp_app.cms.posts.cms_get", return_value=POST_DATA)
    def test_dry_run_default_shows_preview(self, mock_get):
        result = self._call({"id": 99})
        self.assertTrue(result.get("dry_run"))
        self.assertIn("preview", result)

    def test_dry_run_false_without_confirm_returns_error(self):
        result = self._call({"id": 99, "dry_run": False})
        self.assertEqual(result["error_type"], "confirmation_required")

    @patch("mcp_app.cms.posts.cms_delete", return_value={"deleted": True})
    def test_dry_run_false_with_confirm_deletes(self, mock_delete):
        _ = self._call({"id": 99, "dry_run": False, "confirm_delete": True})
        mock_delete.assert_called_once_with(CREDS, "/post/99/")


# ── Categories ─────────────────────────────────────────────────────────────────

class CreateCategoryTests(TestCase):
    @patch("mcp_app.cms.categories.cms_post", return_value={"data": {"id": 3}})
    def test_dry_run_true_returns_preview(self, mock_post):
        from mcp_app.cms.categories import create_category
        result = create_category(CREDS, {"name": "Tech", "dry_run": True})
        mock_post.assert_not_called()
        self.assertTrue(result.get("dry_run"))

    @patch("mcp_app.cms.categories.cms_post", return_value={"data": {"id": 3}})
    def test_dry_run_false_calls_post(self, mock_post):
        from mcp_app.cms.categories import create_category
        create_category(CREDS, {"name": "Tech", "dry_run": False})
        mock_post.assert_called_once()


class DeleteCategoryTests(TestCase):
    @patch("mcp_app.cms.categories.cms_get", return_value={"id": 3, "name": "Tech"})
    def test_dry_run_default_shows_preview(self, _mock):
        from mcp_app.cms.categories import delete_category
        result = delete_category(CREDS, {"id": 3})
        self.assertTrue(result.get("dry_run"))

    def test_no_confirm_returns_confirmation_error(self):
        from mcp_app.cms.categories import delete_category
        result = delete_category(CREDS, {"id": 3, "dry_run": False})
        self.assertEqual(result["error_type"], "confirmation_required")

    @patch("mcp_app.cms.categories.cms_delete", return_value={"deleted": True})
    def test_confirmed_delete_calls_cms_delete(self, mock_delete):
        from mcp_app.cms.categories import delete_category
        delete_category(CREDS, {"id": 3, "dry_run": False, "confirm_delete": True})
        mock_delete.assert_called_once_with(CREDS, "/category/3/")


# ── Tags ───────────────────────────────────────────────────────────────────────

class CreateTagTests(TestCase):
    @patch("mcp_app.cms.tags.cms_post", return_value={"data": {"id": 7}})
    def test_dry_run_true_returns_preview(self, mock_post):
        from mcp_app.cms.tags import create_tag
        result = create_tag(CREDS, {"name": "Breaking", "dry_run": True})
        mock_post.assert_not_called()
        self.assertTrue(result.get("dry_run"))

    @patch("mcp_app.cms.tags.cms_post", return_value={"data": {"id": 7}})
    def test_dry_run_false_calls_post(self, mock_post):
        from mcp_app.cms.tags import create_tag
        create_tag(CREDS, {"name": "Breaking", "dry_run": False})
        mock_post.assert_called_once()


# ── Helpers ────────────────────────────────────────────────────────────────────

class PreviewHelpersTests(TestCase):
    def test_preview_create_contains_resource_name(self):
        from mcp_app.cms.helpers import preview_create_op
        text = preview_create_op("Post", {"title": "Hello", "status": "Published"})
        self.assertIn("Create Post", text)
        self.assertIn("title", text)
        self.assertIn("dry_run=false", text)

    def test_preview_update_shows_field_diff(self):
        from mcp_app.cms.helpers import preview_update_op
        text = preview_update_op("Post", 5, {"title": "Old"}, {"title": "New"})
        self.assertIn("Old", text)
        self.assertIn("New", text)

    def test_preview_delete_contains_warning(self):
        from mcp_app.cms.helpers import preview_delete_op
        text = preview_delete_op("Post", 5, {"id": 5}, warning="CANNOT be undone.")
        self.assertIn("CANNOT be undone", text)
        self.assertIn("confirm_delete=true", text)

    def test_deletion_requires_confirmation_sentinel(self):
        from mcp_app.cms.helpers import DELETION_REQUIRES_CONFIRMATION
        self.assertEqual(DELETION_REQUIRES_CONFIRMATION["error_type"], "confirmation_required")
        self.assertFalse(DELETION_REQUIRES_CONFIRMATION["retryable"])
