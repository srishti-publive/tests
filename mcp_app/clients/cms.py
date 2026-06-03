"""HTTP client for the Publive CMS (Content Management System) API.

Covers GET / POST / PATCH / DELETE.  No automatic retry (write operations are not idempotent).
All error paths return a normalised error dict instead of raising so callers can inspect the
error_type and decide whether to surface a message or propagate.
"""
import logging
import time

import newrelic.agent
import requests

from ..nr_utils import add_attrs, notice_err, record_metric
from .shared import build_base_url, build_basic_auth_headers, slugify_url_path

logger = logging.getLogger(__name__)

_CMS_BASE        = "https://cms-beta.thepublive.com/publisher/{publisher_id}"
_REQUEST_TIMEOUT = 10  # seconds


def classify_cms_error(exc, http_status) -> str:
    """Map a CMS failure to a standard error category string."""
    if isinstance(exc, requests.exceptions.Timeout) or http_status == 408:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status == 404:
        return "not_found"
    if http_status and 400 <= http_status < 500:
        return "bad_request"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"


def normalize_cms_error(exc, url: str) -> dict:
    """Convert an HTTP exception into a structured error dict."""
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
        return {"error_type": "not_found", "message": f"Resource not found ({url}).", "retryable": False}

    if http_status and 400 <= http_status < 500:
        msg      = f"HTTP {http_status}"
        raw_body = ""
        try:
            raw_body = exc.response.text[:1000]
            data     = exc.response.json()
            msg      = (
                data.get("detail")
                or data.get("message")
                or (data.get("error") or {}).get("description")
                or msg
            )
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
        return {"error_type": "bad_request", "message": msg, "raw_api_response": raw_body, "retryable": False}

    if http_status and 500 <= http_status < 600:
        return {"error_type": "upstream_error", "message": f"CMS server error (HTTP {http_status}). Try again shortly.", "retryable": True}

    return {"error_type": "system_error", "message": str(exc), "retryable": False}


def _record_cms_attrs(path: str, method: str, publisher_id: str, http_status: int, latency_ms: float, response_size: int = 0):
    add_attrs([
        ("cms.path",                path),
        ("cms.method",              method),
        ("cms.publisher_id",        publisher_id),
        ("cms.http_status",         http_status),
        ("cms.latency_ms",          latency_ms),
        ("cms.response_size_bytes", response_size),
    ])


def _record_cms_timeout_attrs(path: str, method: str):
    add_attrs([("cms.path", path), ("cms.method", method), ("cms.timed_out", True)])
    record_metric("Custom/CMS/timeout_count", 1)
    record_metric("Custom/CMS/error_count",   1)


@newrelic.agent.function_trace(name="cms_get", group="CMS")
def cms_get(credentials: dict, path: str, params=None):
    url          = build_base_url(_CMS_BASE, credentials) + path
    publisher_id = credentials.get("publisherId", "")
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    newrelic.agent.add_custom_span_attribute("cms.url",          url)
    newrelic.agent.add_custom_span_attribute("cms.path",         path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp       = requests.get(url, headers=build_basic_auth_headers(credentials), params=clean_params, timeout=_REQUEST_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc          = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            add_attrs([("cms.path", path), ("cms.method", "GET"), ("cms.http_status", resp.status_code), ("cms.latency_ms", latency_ms)])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", classify_cms_error(exc, resp.status_code))])
            return normalize_cms_error(exc, url)
        response_size = len(resp.content)
        _record_cms_attrs(path, "GET", publisher_id, resp.status_code, latency_ms, response_size)
        record_metric("Custom/CMS/latency_ms",           latency_ms)
        record_metric("Custom/CMS/response_size_bytes",  response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        _record_cms_timeout_attrs(path, "GET")
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_get unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_post", group="CMS")
def cms_post(credentials: dict, path: str, body: dict):
    url          = build_base_url(_CMS_BASE, credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url",          url)
    newrelic.agent.add_custom_span_attribute("cms.path",         path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp       = requests.post(url, headers=build_basic_auth_headers(credentials), json=body, timeout=_REQUEST_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc          = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            add_attrs([("cms.path", path), ("cms.method", "POST"), ("cms.http_status", resp.status_code), ("cms.latency_ms", latency_ms)])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", classify_cms_error(exc, resp.status_code))])
            return normalize_cms_error(exc, url)
        response_size = len(resp.content)
        _record_cms_attrs(path, "POST", publisher_id, resp.status_code, latency_ms, response_size)
        record_metric("Custom/CMS/latency_ms",           latency_ms)
        record_metric("Custom/CMS/response_size_bytes",  response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        _record_cms_timeout_attrs(path, "POST")
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_post unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_patch", group="CMS")
def cms_patch(credentials: dict, path: str, body: dict):
    url          = build_base_url(_CMS_BASE, credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url",          url)
    newrelic.agent.add_custom_span_attribute("cms.path",         path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp       = requests.patch(url, headers=build_basic_auth_headers(credentials), json=body, timeout=_REQUEST_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc          = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            add_attrs([("cms.path", path), ("cms.method", "PATCH"), ("cms.http_status", resp.status_code), ("cms.latency_ms", latency_ms)])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", classify_cms_error(exc, resp.status_code))])
            return normalize_cms_error(exc, url)
        response_size = len(resp.content)
        _record_cms_attrs(path, "PATCH", publisher_id, resp.status_code, latency_ms, response_size)
        record_metric("Custom/CMS/latency_ms",           latency_ms)
        record_metric("Custom/CMS/response_size_bytes",  response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        _record_cms_timeout_attrs(path, "PATCH")
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_patch unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_delete", group="CMS")
def cms_delete(credentials: dict, path: str):
    url          = build_base_url(_CMS_BASE, credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url",          url)
    newrelic.agent.add_custom_span_attribute("cms.path",         path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp       = requests.delete(url, headers=build_basic_auth_headers(credentials), timeout=_REQUEST_TIMEOUT)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc          = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            add_attrs([("cms.path", path), ("cms.method", "DELETE"), ("cms.http_status", resp.status_code), ("cms.latency_ms", latency_ms)])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", classify_cms_error(exc, resp.status_code))])
            return normalize_cms_error(exc, url)
        response_size = len(resp.content) if resp.content else 0
        _record_cms_attrs(path, "DELETE", publisher_id, resp.status_code, latency_ms, response_size)
        record_metric("Custom/CMS/latency_ms",           latency_ms)
        record_metric("Custom/CMS/response_size_bytes",  response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        _record_cms_timeout_attrs(path, "DELETE")
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_delete unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise
