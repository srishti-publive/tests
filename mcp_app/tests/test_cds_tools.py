"""Unit tests for all CDS read tool handlers.

All tests mock cds_get so no real HTTP calls are made. Each test verifies:
  - the correct path and params are passed to cds_get
  - the handler returns the upstream response unchanged
  - edge-case branches (empty legacy_url, timeout fallback, wrong post type) behave correctly
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

CREDS = {"publisherId": "pub1", "apiKey": "key", "apiSecret": "secret"}
OK_RESPONSE = {"data": [{"id": 1}]}


class FetchPublishedPostsTests(TestCase):
    def _call(self, args):
        from mcp_app.cds.posts import fetch_published_posts
        return fetch_published_posts(CREDS, args)

    @patch("mcp_app.cds.posts.cds_get", return_value=OK_RESPONSE)
    def test_passes_page_and_limit(self, mock_get):
        result = self._call({"page": 2, "limit": 5})
        mock_get.assert_called_once_with(CREDS, "/posts/", {"page": 2, "limit": 5})
        self.assertEqual(result, OK_RESPONSE)

    @patch("mcp_app.cds.posts.cds_get", return_value=OK_RESPONSE)
    def test_passes_extra_filters(self, mock_get):
        self._call({"page": 1, "limit": 10, "type__eq": "Article"})
        _, _, params = mock_get.call_args[0]
        self.assertEqual(params["type__eq"], "Article")

    @patch("mcp_app.cds.posts.cds_get", side_effect=Exception("timed out"))
    def test_timeout_returns_structured_error(self, _mock):
        result = self._call({"page": 1, "limit": 10})
        self.assertEqual(result["error"], "upstream_timeout")
        self.assertTrue(result["retry"])

    @patch("mcp_app.cds.posts.cds_get", side_effect=Exception("unexpected"))
    def test_non_timeout_exception_propagates(self, _mock):
        with self.assertRaises(Exception):
            self._call({"page": 1, "limit": 10})


class FetchPublishedPostTests(TestCase):
    @patch("mcp_app.cds.posts.cds_get", return_value={"id": 42})
    def test_fetches_by_identifier(self, mock_get):
        from mcp_app.cds.posts import fetch_published_post
        result = fetch_published_post(CREDS, {"identifier": "my-slug"})
        mock_get.assert_called_once_with(CREDS, "/post/my-slug/")
        self.assertEqual(result, {"id": 42})


class FetchPostByUrlTests(TestCase):
    def _call(self, args):
        from mcp_app.cds.posts import fetch_post_by_url
        return fetch_post_by_url(CREDS, args)

    @patch("mcp_app.cds.posts.cds_get", return_value=OK_RESPONSE)
    def test_passes_legacy_url(self, mock_get):
        self._call({"legacy_url": "/business/article-123"})
        mock_get.assert_called_once_with(CREDS, "/post/", {"legacy_url": "/business/article-123"})

    def test_empty_legacy_url_returns_error(self):
        result = self._call({"legacy_url": ""})
        self.assertEqual(result["error"], "invalid_input")

    def test_missing_legacy_url_returns_error(self):
        result = self._call({})
        self.assertEqual(result["error"], "invalid_input")


class FetchLiveblogWithUpdatesTests(TestCase):
    def _call(self, args):
        from mcp_app.cds.posts import fetch_liveblog_with_updates
        return fetch_liveblog_with_updates(CREDS, args)

    @patch("mcp_app.cds.posts.cds_get")
    def test_returns_combined_post_and_updates(self, mock_get):
        mock_get.side_effect = [
            {"data": {"type": "LiveBlog", "id": 5}},
            {"data": []},
        ]
        result = self._call({"post_id": 5})
        self.assertIn("post", result)
        self.assertIn("updates", result)

    @patch("mcp_app.cds.posts.cds_get")
    def test_wrong_type_returns_error(self, mock_get):
        mock_get.return_value = {"data": {"type": "Article"}}
        result = self._call({"post_id": 10})
        self.assertEqual(result["error"], "invalid_input")

    @patch("mcp_app.cds.posts.cds_get")
    def test_upstream_error_on_post_fetch_returns_error(self, mock_get):
        mock_get.return_value = {"error_type": "not_found", "message": "404"}
        result = self._call({"post_id": 999})
        self.assertEqual(result["error_type"], "not_found")


class FetchTrendingPostsTests(TestCase):
    @patch("mcp_app.cds.posts.cds_get", return_value=OK_RESPONSE)
    def test_passes_params(self, mock_get):
        from mcp_app.cds.posts import fetch_trending_posts
        fetch_trending_posts(CREDS, {"duration": "7d", "limit": 10, "page": 1})
        _, _, params = mock_get.call_args[0]
        self.assertEqual(params["duration"], "7d")
        self.assertEqual(params["limit"], 10)


class FetchPublishedCategoriesTests(TestCase):
    @patch("mcp_app.cds.categories.cds_get", return_value=OK_RESPONSE)
    def test_passes_page_limit(self, mock_get):
        from mcp_app.cds.categories import fetch_published_categories
        fetch_published_categories(CREDS, {"page": 3, "limit": 20})
        mock_get.assert_called_once_with(CREDS, "/categories/", {"page": 3, "limit": 20})

    @patch("mcp_app.cds.categories.cds_get", return_value={"id": 7})
    def test_fetch_single_category(self, mock_get):
        from mcp_app.cds.categories import fetch_published_category
        result = fetch_published_category(CREDS, {"identifier": "sports"})
        mock_get.assert_called_once_with(CREDS, "/category/sports/")
        self.assertEqual(result, {"id": 7})


class FetchPublishedTagsTests(TestCase):
    @patch("mcp_app.cds.tags.cds_get", return_value=OK_RESPONSE)
    def test_list_tags(self, mock_get):
        from mcp_app.cds.tags import fetch_published_tags
        fetch_published_tags(CREDS, {"page": 1, "limit": 10})
        mock_get.assert_called_once_with(CREDS, "/tags/", {"page": 1, "limit": 10})

    @patch("mcp_app.cds.tags.cds_get", return_value={"id": 3})
    def test_fetch_single_tag(self, mock_get):
        from mcp_app.cds.tags import fetch_published_tag
        result = fetch_published_tag(CREDS, {"identifier": "breaking-news"})
        mock_get.assert_called_once_with(CREDS, "/tag/breaking-news/")
        self.assertEqual(result, {"id": 3})


class FetchAuthorsTests(TestCase):
    @patch("mcp_app.cds.authors.cds_get", return_value=OK_RESPONSE)
    def test_list_authors(self, mock_get):
        from mcp_app.cds.authors import fetch_authors
        fetch_authors(CREDS, {"page": 1})
        mock_get.assert_called_once_with(CREDS, "/authors/", {"page": 1, "limit": None})

    @patch("mcp_app.cds.authors.cds_get", return_value={"id": 12})
    def test_fetch_single_author_by_numeric_id(self, mock_get):
        from mcp_app.cds.authors import fetch_author
        fetch_author(CREDS, {"identifier": "42"})
        mock_get.assert_called_once_with(CREDS, "/author/42/")

    def test_fetch_author_non_numeric_id_returns_error(self):
        from mcp_app.cds.authors import fetch_author
        result = fetch_author(CREDS, {"identifier": "jane-doe"})
        self.assertEqual(result["error"], "invalid_input")

    def test_fetch_author_empty_identifier_returns_error(self):
        from mcp_app.cds.authors import fetch_author
        result = fetch_author(CREDS, {"identifier": ""})
        self.assertEqual(result["error"], "invalid_input")
