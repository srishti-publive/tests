import logging
import time

import newrelic.agent
import requests

from .nr_utils import add_attrs, notice_err, record_metric
from .utils import classify_error_category, make_basic_token, require_publisher_id

logger = logging.getLogger(__name__)

_CMS_BASE        = "https://cms-beta.thepublive.com/publisher/{publisher_id}"
_REQUEST_TIMEOUT = 10


# ── Shared error return values ────────────────────────────────────────────────
# Module-level constants prevent the same dict from being copy-pasted into each
# of the four HTTP verb functions.  One definition → one edit point if the
# shape or message ever changes.

_CMS_TIMEOUT_ERROR: dict = {
    "error_type": "timeout",
    "message":    "CMS request timed out.",
    "retryable":  True,
}

_CMS_CONNECTION_ERROR: dict = {
    "error_type": "system_error",
    "message":    "Could not connect to CMS API.",
    "retryable":  True,
}


# ── Auth & URL helpers ────────────────────────────────────────────────────────

def _base_url(credentials: dict) -> str:
    publisher_id = require_publisher_id(credentials)
    return _CMS_BASE.format(publisher_id=publisher_id)


def _auth_headers(credentials: dict) -> dict:
    token = make_basic_token(
        credentials.get("apiKey", ""),
        credentials.get("apiSecret", ""),
    )
    return {
        "Authorization": f"Basic {token}",
        "Content-Type":  "application/json",
    }


# ── Error normalisation ───────────────────────────────────────────────────────

def _handle_error(exc, url: str) -> dict:
    """Translate an HTTP error response into a normalised error dict.

    Note: error_type values here are user-/AI-facing (not_found, bad_request …).
    NR error.category is set separately via classify_error_category() in _cms_request.
    """
    http_status = getattr(getattr(exc, "response", None), "status_code", None)

    if http_status == 401:
        return {
            "error_type": "auth_error",
            "message": (
                "CMS credentials rejected (HTTP 401). "
                "Please re-authenticate: visit /connect or re-run the OAuth flow."
            ),
            "retryable": False,
        }
    if http_status == 404:
        return {
            "error_type": "not_found",
            "message":    f"Resource not found ({url}).",
            "retryable":  False,
        }
    if http_status and 400 <= http_status < 500:
        msg      = f"HTTP {http_status}"
        raw_body = ""
        try:
            raw_body = exc.response.text[:1000]
            data = exc.response.json()
            # Try standard error envelope fields first
            msg = (
                data.get("detail")
                or data.get("message")
                or (data.get("error") or {}).get("description")
                or msg
            )
            # DRF field-level validation errors look like {"field": ["msg", ...]}
            if msg == f"HTTP {http_status}" and isinstance(data, dict):
                field_errors = []
                for key, val in data.items():
                    if isinstance(val, list):
                        field_errors.append(f"{key}: {', '.join(str(v) for v in val)}")
                    elif isinstance(val, str):
                        field_errors.append(f"{key}: {val}")
                if field_errors:
                    msg = "Validation error — " + "; ".join(field_errors)
        except Exception:
            pass
        logger.warning("cms_client 4xx: url=%s status=%d raw_body=%s", url, http_status, raw_body)
        return {
            "error_type":       "bad_request",
            "message":          msg,
            "raw_api_response": raw_body,
            "retryable":        False,
        }
    if http_status and 500 <= http_status < 600:
        return {
            "error_type": "upstream_error",
            "message":    f"CMS server error (HTTP {http_status}). Try again shortly.",
            "retryable":  True,
        }
    return {
        "error_type": "system_error",
        "message":    str(exc),
        "retryable":  False,
    }


# ── Core HTTP dispatcher ──────────────────────────────────────────────────────

def _cms_request(credentials: dict, method: str, path: str, *, body=None, params=None):
    """Single implementation for all CMS HTTP verbs.

    Previously, cms_get / cms_post / cms_patch / cms_delete each contained an
    identical ~55-line body.  Any cross-cutting change (timeout value, new span
    attribute, error-handling tweak) had to be applied four times.  cms_delete
    had already drifted (extra try/except around len(resp.content)).  This
    function is the single authoritative implementation; the four public
    functions below are thin NR-traced wrappers over it.

    Args:
        method:  Uppercase HTTP verb — "GET", "POST", "PATCH", or "DELETE".
        body:    JSON-serialisable dict sent as the request body (POST/PATCH).
        params:  Query-string dict (GET).  None values are stripped.
    """
    url          = _base_url(credentials) + path
    publisher_id = credentials.get("publisherId", "")

    # Span-level attributes — visible in NR distributed-tracing waterfall.
    # Set once here instead of once per verb function.
    newrelic.agent.add_custom_span_attribute("cms.url", url)
    newrelic.agent.add_custom_span_attribute("cms.path", path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)

    request_kwargs: dict = {
        "headers": _auth_headers(credentials),
        "timeout": _REQUEST_TIMEOUT,
    }
    if body is not None:
        request_kwargs["json"] = body
    if params is not None:
        request_kwargs["params"] = {k: v for k, v in params.items() if v is not None}

    t0 = time.perf_counter()
    try:
        resp = getattr(requests, method.lower())(url, **request_kwargs)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp  # type: ignore[attr-defined]
            error_category = classify_error_category(exc, resp.status_code)
            add_attrs([
                ("cms.path",        path),
                ("cms.method",      method),
                ("cms.http_status", resp.status_code),
                ("cms.latency_ms",  latency_ms),
                ("error.category",  error_category),
            ])
            record_metric("Custom/CMS/error_count", 1)
            return _handle_error(exc, url)

        # Safe for all verbs, including DELETE (some APIs return 204 with no body).
        try:
            response_size = len(resp.content)
        except Exception:
            response_size = 0

        add_attrs([
            ("cms.path",                 path),
            ("cms.method",               method),
            ("cms.publisher_id",         publisher_id),
            ("cms.http_status",          resp.status_code),
            ("cms.latency_ms",           latency_ms),
            ("cms.response_size_bytes",  response_size),
        ])
        record_metric("Custom/CMS/latency_ms", latency_ms)
        record_metric("Custom/CMS/response_size_bytes", response_size)
        return resp.json()

    except requests.exceptions.Timeout:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        add_attrs([
            ("cms.path",      path),
            ("cms.method",    method),
            ("cms.timed_out", True),
        ])
        record_metric("Custom/CMS/timeout_count", 1)
        record_metric("Custom/CMS/error_count", 1)
        return _CMS_TIMEOUT_ERROR

    except requests.exceptions.ConnectionError:
        return _CMS_CONNECTION_ERROR

    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error(
            "cms_%s: unexpected error: path=%s error=%s",
            method.lower(), path, exc, exc_info=True,
        )
        raise


# ── Public API — thin NR-traced wrappers over _cms_request ───────────────────

@newrelic.agent.function_trace(name="cms_get", group="CMS")
def cms_get(credentials, path, params=None):
    return _cms_request(credentials, "GET", path, params=params)


@newrelic.agent.function_trace(name="cms_post", group="CMS")
def cms_post(credentials, path, body):
    return _cms_request(credentials, "POST", path, body=body)


@newrelic.agent.function_trace(name="cms_patch", group="CMS")
def cms_patch(credentials, path, body):
    return _cms_request(credentials, "PATCH", path, body=body)


@newrelic.agent.function_trace(name="cms_delete", group="CMS")
def cms_delete(credentials, path):
    return _cms_request(credentials, "DELETE", path)
