import base64
import logging
import time

import newrelic.agent
import requests

from .nr_utils import add_attrs, notice_err, record_metric

logger = logging.getLogger(__name__)

_CMS_BASE = "https://cms.thepublive.com/publisher/{publisher_id}"
_REQUEST_TIMEOUT = 10


def _slugify_path(path):
    slug = path.strip("/").replace("/", "_")
    return slug or "root"


def _cms_error_category(exc, http_status):
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


# ── Auth & URL helpers ────────────────────────────────────────────────────────

def _base_url(credentials):
    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")
    return _CMS_BASE.format(publisher_id=publisher_id)


def _auth_headers(credentials):
    api_key    = credentials.get("apiKey", "")
    api_secret = credentials.get("apiSecret", "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


# ── Error normalisation ───────────────────────────────────────────────────────

def _handle_error(exc, url):
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
            "message": f"Resource not found ({url}).",
            "retryable": False,
        }
    if http_status and 400 <= http_status < 500:
        msg = f"HTTP {http_status}"
        try:
            data = exc.response.json()
            msg = (
                data.get("detail")
                or data.get("message")
                or (data.get("error") or {}).get("description")
                or msg
            )
        except Exception:
            pass
        return {
            "error_type": "bad_request",
            "message": msg,
            "retryable": False,
        }
    if http_status and 500 <= http_status < 600:
        return {
            "error_type": "upstream_error",
            "message": f"CMS server error (HTTP {http_status}). Try again shortly.",
            "retryable": True,
        }
    return {
        "error_type": "system_error",
        "message": str(exc),
        "retryable": False,
    }


# ── HTTP verbs ────────────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="cms_get", group="CMS")
def cms_get(credentials, path, params=None):
    url = _base_url(credentials) + path
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url", url)
    newrelic.agent.add_custom_span_attribute("cms.path", path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp = requests.get(
            url,
            headers=_auth_headers(credentials),
            params=clean_params,
            timeout=_REQUEST_TIMEOUT,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            error_category = _cms_error_category(exc, resp.status_code)
            add_attrs([
                ("cms.path", path),
                ("cms.method", "GET"),
                ("cms.http_status", resp.status_code),
                ("cms.latency_ms", latency_ms),
            ])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", error_category)])
            return _handle_error(exc, url)
        response_size = len(resp.content)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "GET"),
            ("cms.publisher_id", publisher_id),
            ("cms.http_status", resp.status_code),
            ("cms.latency_ms", latency_ms),
            ("cms.response_size_bytes", response_size),
        ])
        record_metric("Custom/CMS/latency_ms", latency_ms)
        record_metric("Custom/CMS/response_size_bytes", response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "GET"),
            ("cms.timed_out", True),
        ])
        record_metric("Custom/CMS/timeout_count", 1)
        record_metric("Custom/CMS/error_count", 1)
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_get: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_post", group="CMS")
def cms_post(credentials, path, body):
    url = _base_url(credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url", url)
    newrelic.agent.add_custom_span_attribute("cms.path", path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            url,
            headers=_auth_headers(credentials),
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            error_category = _cms_error_category(exc, resp.status_code)
            add_attrs([
                ("cms.path", path),
                ("cms.method", "POST"),
                ("cms.http_status", resp.status_code),
                ("cms.latency_ms", latency_ms),
            ])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", error_category)])
            return _handle_error(exc, url)
        response_size = len(resp.content)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "POST"),
            ("cms.publisher_id", publisher_id),
            ("cms.http_status", resp.status_code),
            ("cms.latency_ms", latency_ms),
            ("cms.response_size_bytes", response_size),
        ])
        record_metric("Custom/CMS/latency_ms", latency_ms)
        record_metric("Custom/CMS/response_size_bytes", response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "POST"),
            ("cms.timed_out", True),
        ])
        record_metric("Custom/CMS/timeout_count", 1)
        record_metric("Custom/CMS/error_count", 1)
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_post: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_patch", group="CMS")
def cms_patch(credentials, path, body):
    url = _base_url(credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url", url)
    newrelic.agent.add_custom_span_attribute("cms.path", path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp = requests.patch(
            url,
            headers=_auth_headers(credentials),
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            error_category = _cms_error_category(exc, resp.status_code)
            add_attrs([
                ("cms.path", path),
                ("cms.method", "PATCH"),
                ("cms.http_status", resp.status_code),
                ("cms.latency_ms", latency_ms),
            ])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", error_category)])
            return _handle_error(exc, url)
        response_size = len(resp.content)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "PATCH"),
            ("cms.publisher_id", publisher_id),
            ("cms.http_status", resp.status_code),
            ("cms.latency_ms", latency_ms),
            ("cms.response_size_bytes", response_size),
        ])
        record_metric("Custom/CMS/latency_ms", latency_ms)
        record_metric("Custom/CMS/response_size_bytes", response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "PATCH"),
            ("cms.timed_out", True),
        ])
        record_metric("Custom/CMS/timeout_count", 1)
        record_metric("Custom/CMS/error_count", 1)
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_patch: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


@newrelic.agent.function_trace(name="cms_delete", group="CMS")
def cms_delete(credentials, path):
    url = _base_url(credentials) + path
    publisher_id = credentials.get("publisherId", "")
    newrelic.agent.add_custom_span_attribute("cms.url", url)
    newrelic.agent.add_custom_span_attribute("cms.path", path)
    newrelic.agent.add_custom_span_attribute("cms.publisher_id", publisher_id)
    t0 = time.perf_counter()
    try:
        resp = requests.delete(
            url,
            headers=_auth_headers(credentials),
            timeout=_REQUEST_TIMEOUT,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp
            error_category = _cms_error_category(exc, resp.status_code)
            add_attrs([
                ("cms.path", path),
                ("cms.method", "DELETE"),
                ("cms.http_status", resp.status_code),
                ("cms.latency_ms", latency_ms),
            ])
            record_metric("Custom/CMS/error_count", 1)
            add_attrs([("error.category", error_category)])
            return _handle_error(exc, url)
        try:
            response_size = len(resp.content)
        except Exception:
            response_size = 0
        add_attrs([
            ("cms.path", path),
            ("cms.method", "DELETE"),
            ("cms.publisher_id", publisher_id),
            ("cms.http_status", resp.status_code),
            ("cms.latency_ms", latency_ms),
            ("cms.response_size_bytes", response_size),
        ])
        record_metric("Custom/CMS/latency_ms", latency_ms)
        record_metric("Custom/CMS/response_size_bytes", response_size)
        return resp.json()
    except requests.exceptions.Timeout:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        add_attrs([
            ("cms.path", path),
            ("cms.method", "DELETE"),
            ("cms.timed_out", True),
        ])
        record_metric("Custom/CMS/timeout_count", 1)
        record_metric("Custom/CMS/error_count", 1)
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        notice_err(exc, [("error.layer", "cms"), ("error.cms_path", path)])
        logger.error("cms_delete: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise
