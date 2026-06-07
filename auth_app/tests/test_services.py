import json

from django.test import RequestFactory, TestCase, override_settings

from auth_app.services import (
    check_origin,
    is_loopback_redirect_uri,
    parse_oauth_token_body,
    redirect_uri_is_registered,
    redirect_uris_match,
    validate_redirect_uris,
)


class CheckOriginTests(TestCase):
    def _request(self, origin=None):
        factory = RequestFactory()
        req = factory.get("/")
        if origin:
            req.META["HTTP_ORIGIN"] = origin
        return req

    def test_no_origin_is_allowed(self):
        self.assertIsNone(check_origin(self._request()))

    @override_settings(BASE_URL="https://mcp.example.com", OAUTH_ALLOWED_ORIGINS=["https://claude.ai"])
    def test_allowed_origin_is_accepted(self):
        self.assertIsNone(check_origin(self._request(origin="https://claude.ai")))

    @override_settings(BASE_URL="https://mcp.example.com", OAUTH_ALLOWED_ORIGINS=["https://claude.ai"])
    def test_base_url_same_origin_is_accepted(self):
        self.assertIsNone(check_origin(self._request(origin="https://mcp.example.com")))

    @override_settings(BASE_URL="https://mcp.example.com", OAUTH_ALLOWED_ORIGINS=["https://claude.ai"])
    def test_unknown_origin_is_blocked(self):
        resp = check_origin(self._request(origin="https://evil.com"))
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 403)

    @override_settings(BASE_URL="https://mcp.example.com/", OAUTH_ALLOWED_ORIGINS=[])
    def test_trailing_slash_stripped_from_base_url(self):
        # BASE_URL with trailing slash should still match same-origin request
        self.assertIsNone(check_origin(self._request(origin="https://mcp.example.com")))

    @override_settings(BASE_URL="https://mcp.example.com", OAUTH_ALLOWED_ORIGINS=["https://claude.ai"])
    def test_origin_trailing_slash_stripped(self):
        # Origin headers sometimes include a trailing slash
        self.assertIsNone(check_origin(self._request(origin="https://claude.ai/")))


class ValidateRedirectUrisTests(TestCase):
    @override_settings(OAUTH_ALLOWED_REDIRECT_URIS=["https://claude.ai/api/mcp/auth_callback"])
    def test_allowed_uri_accepted(self):
        self.assertTrue(validate_redirect_uris(["https://claude.ai/api/mcp/auth_callback"]))

    @override_settings(OAUTH_ALLOWED_REDIRECT_URIS=["https://claude.ai/api/mcp/auth_callback"])
    def test_disallowed_uri_rejected(self):
        self.assertFalse(validate_redirect_uris(["https://evil.com/callback"]))

    @override_settings(OAUTH_ALLOWED_REDIRECT_URIS=["https://claude.ai/api/mcp/auth_callback"])
    def test_empty_list_is_valid(self):
        self.assertTrue(validate_redirect_uris([]))

    @override_settings(OAUTH_ALLOWED_REDIRECT_URIS=["https://claude.ai/api/mcp/auth_callback"])
    def test_mixed_list_rejected_if_any_disallowed(self):
        self.assertFalse(
            validate_redirect_uris([
                "https://claude.ai/api/mcp/auth_callback",
                "https://evil.com/callback",
            ])
        )


class RedirectUriIsRegisteredTests(TestCase):
    def test_exact_match_accepted(self):
        self.assertTrue(
            redirect_uri_is_registered(
                "https://claude.ai/api/mcp/auth_callback",
                ["https://claude.ai/api/mcp/auth_callback"],
            )
        )

    def test_non_matching_uri_rejected(self):
        self.assertFalse(
            redirect_uri_is_registered(
                "https://evil.com/callback",
                ["https://claude.ai/api/mcp/auth_callback"],
            )
        )

    def test_empty_registered_list_rejects_all(self):
        self.assertFalse(redirect_uri_is_registered("https://claude.ai/callback", []))


class IsLoopbackRedirectUriTests(TestCase):
    def test_localhost_accepted(self):
        self.assertTrue(is_loopback_redirect_uri("http://localhost:38967/oauth/callback"))

    def test_127_0_0_1_accepted(self):
        self.assertTrue(is_loopback_redirect_uri("http://127.0.0.1:5173/callback"))

    def test_ipv6_loopback_accepted(self):
        self.assertTrue(is_loopback_redirect_uri("http://[::1]:5173/callback"))

    def test_https_loopback_rejected(self):
        # Native apps use plain http for their local callback listener.
        self.assertFalse(is_loopback_redirect_uri("https://localhost:5173/callback"))

    def test_remote_host_rejected(self):
        self.assertFalse(is_loopback_redirect_uri("http://claude.ai/callback"))

    def test_malformed_uri_rejected(self):
        self.assertFalse(is_loopback_redirect_uri("not-a-uri"))


class RedirectUrisMatchTests(TestCase):
    def test_exact_match_accepted(self):
        self.assertTrue(redirect_uris_match("https://claude.ai/api/mcp/auth_callback", "https://claude.ai/api/mcp/auth_callback"))

    def test_loopback_uris_differing_only_by_port_match(self):
        self.assertTrue(
            redirect_uris_match(
                "http://localhost:38967/oauth/callback",
                "http://localhost:9999/oauth/callback",
            )
        )

    def test_loopback_uris_differing_by_path_do_not_match(self):
        self.assertFalse(
            redirect_uris_match(
                "http://localhost:38967/oauth/callback",
                "http://localhost:38967/other/callback",
            )
        )

    def test_loopback_vs_remote_does_not_match(self):
        self.assertFalse(
            redirect_uris_match("http://localhost:38967/callback", "https://claude.ai/api/mcp/auth_callback")
        )

    def test_non_loopback_mismatch_rejected(self):
        self.assertFalse(redirect_uris_match("https://evil.com/callback", "https://claude.ai/api/mcp/auth_callback"))


class ParseOAuthTokenBodyTests(TestCase):
    def _post(self, body, content_type):
        factory = RequestFactory()
        return factory.post("/token", data=body, content_type=content_type)

    def test_json_body_parsed(self):
        req = self._post(json.dumps({"grant_type": "authorization_code"}), "application/json")
        data, err = parse_oauth_token_body(req)
        self.assertIsNone(err)
        self.assertEqual(data["grant_type"], "authorization_code")

    def test_form_encoded_body_parsed(self):
        req = self._post("grant_type=authorization_code&code=abc", "application/x-www-form-urlencoded")
        data, err = parse_oauth_token_body(req)
        self.assertIsNone(err)
        self.assertEqual(data["grant_type"], "authorization_code")
        self.assertEqual(data["code"], "abc")

    def test_unknown_content_type_returns_error(self):
        req = self._post("data", "text/plain")
        data, err = parse_oauth_token_body(req)
        self.assertIsNone(data)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_invalid_json_returns_error(self):
        req = self._post("not json", "application/json")
        data, err = parse_oauth_token_body(req)
        self.assertIsNone(data)
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_json_with_charset_parsed(self):
        req = self._post(json.dumps({"grant_type": "refresh_token"}), "application/json; charset=utf-8")
        data, err = parse_oauth_token_body(req)
        self.assertIsNone(err)
        self.assertEqual(data["grant_type"], "refresh_token")
