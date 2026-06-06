"""Middleware stack for the Publive MCP server.

RateLimitMiddleware   — sliding-window rate limits on auth and MCP endpoints
SecurityHeadersMiddleware — CSP + X-Frame-Options + nosniff on every HTML response
"""
import logging
import time
from typing import Optional
import uuid

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse

logger = logging.getLogger(__name__)

# (path_prefix, http_method_or_None, limit, window_seconds, id_strategy)
# method=None matches any HTTP method.
_RULES: list[tuple[str, Optional[str], int, int, str]] = [
    ("/auth/login",  "POST", 10,  60, "ip"),     # login brute-force
    ("/register",    "POST", 20,  60, "ip"),      # client registration
    ("/authorize",   None,   20,  60, "ip"),      # OAuth authorize
    ("/token",       "POST", 20,  60, "ip"),      # token exchange/refresh
    ("/mcp",         None,   300, 60, "token"),   # MCP tool calls
]


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _identifier(request, strategy: str) -> str:
    if strategy == "token":
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if auth.startswith("Bearer ") and len(auth) > 14:
            # Use first 12 chars of the token — enough for bucketing, not full exposure in cache
            return "tok:" + auth[7:19]
    return "ip:" + _client_ip(request)


class RateLimitMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "RATE_LIMIT_ENABLED", True):
            return self.get_response(request)

        path   = request.path
        method = request.method

        for prefix, rule_method, limit, window, strategy in _RULES:
            if not path.startswith(prefix):
                continue
            if rule_method is not None and method != rule_method:
                continue

            ident      = _identifier(request, strategy)
            slot       = int(time.time()) // window
            cache_key  = f"rl:{prefix}:{ident}:{slot}"

            try:
                count = cache.get(cache_key, 0)
                if count >= limit:
                    retry_after = window - (int(time.time()) % window)
                    logger.warning(
                        "rate_limit_exceeded path=%s ident=%s count=%d limit=%d",
                        path, ident[:20], count, limit,
                    )
                    response = JsonResponse(
                        {
                            "error": "rate_limit_exceeded",
                            "error_description": (
                                f"Too many requests. Limit: {limit} per {window}s. "
                                f"Retry after {retry_after}s."
                            ),
                            "retry_after": retry_after,
                        },
                        status=429,
                    )
                    response["Retry-After"] = str(retry_after)
                    return response

                # Increment; TTL is 2× window so the key outlives the window slot
                cache.set(cache_key, count + 1, timeout=window * 2)

            except Exception:  # noqa: BLE001 — fail open; cache backend can raise arbitrary errors
                # Fail open — never block traffic because of a cache outage
                logger.warning("rate_limit cache error for path=%s — failing open", path, exc_info=True)

            break  # only apply first matching rule

        return self.get_response(request)


class RequestIDMiddleware:
    """Attach a request ID to every request/response for log correlation.

    Reads X-Request-ID from the incoming request if present; otherwise generates
    a UUID4. The ID is stored on request.request_id and echoed back in the
    X-Request-ID response header so callers can correlate logs with their requests.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.META.get("HTTP_X_REQUEST_ID", "") or str(uuid.uuid4())
        request.request_id = request_id
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware:
    """Add security headers to every HTML response served by the auth pages.

    Applied only to text/html responses so JSON API endpoints are unaffected.
    Prevents clickjacking (X-Frame-Options), MIME sniffing (nosniff), and
    cross-site scripting via a restrictive Content-Security-Policy.
    """

    _CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "text/html" in response.get("Content-Type", ""):
            response.setdefault("Content-Security-Policy", self._CSP)
            response.setdefault("X-Frame-Options", "DENY")
            response.setdefault("X-Content-Type-Options", "nosniff")
            response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        return response
