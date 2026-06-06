"""HTTP client for the Publive CDS (Content Delivery System) API.

Read-only. Retries once on transient failures (timeout, HTTP 408).
"""
import json
import logging
import time

import newrelic.agent
import requests

from mcp_app.nr_utils import add_attrs, notice_err, set_txn_name

from .shared import build_base_url, build_basic_auth_headers, slugify_url_path

logger = logging.getLogger(__name__)

_CDS_BASE = "https://cds.thepublive.com/publisher/{publisher_id}"
_REQUEST_TIMEOUT = 5   # seconds per attempt
_RETRY_BACKOFF   = 1   # seconds between attempts


def classify_cds_error(exc, http_status) -> str:
    """Map a CDS failure to a standard error category string."""
    if isinstance(exc, requests.exceptions.Timeout) or http_status == 408:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status and 400 <= http_status < 500:
        return "client_error"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"


def is_retryable_cds_error(exc) -> bool:
    """True for transient failures worth retrying once."""
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 408


@newrelic.agent.function_trace(name="cds_get", group="CDS")
def cds_get(credentials: dict, path: str, params=None):
    """GET a CDS endpoint; retry once on timeout or HTTP 408."""
    set_txn_name(f"CDS/{slugify_url_path(path)}", group="CDS")

    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")

    headers = build_basic_auth_headers(credentials)
    url     = build_base_url(_CDS_BASE, credentials) + path

    newrelic.agent.add_custom_span_attribute("cds.url", url)
    newrelic.agent.add_custom_span_attribute("cds.publisher_id", publisher_id)
    newrelic.agent.add_custom_span_attribute("cds.path", path)

    clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}

    t0          = time.perf_counter()
    last_exc    = None
    retry_count = 0

    for attempt in range(2):
        if attempt > 0:
            time.sleep(_RETRY_BACKOFF)
            retry_count = attempt
            logger.warning("CDS retry attempt %d: path=%s publisher=%s", attempt, path, publisher_id)

        try:
            resp = requests.get(
                url,
                headers={"Authorization": headers["Authorization"]},
                params=clean_params,
                timeout=_REQUEST_TIMEOUT,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)

            if not resp.ok:
                try:
                    data = resp.json()
                    msg  = data.get("detail") or data.get("message") or f"HTTP {resp.status_code}"
                except (ValueError, json.JSONDecodeError):
                    msg = f"HTTP {resp.status_code}"
                exc          = Exception(f"{msg} [url={url}]")
                exc.response = resp  # type: ignore[attr-defined]
                if resp.status_code == 408 and attempt == 0:
                    last_exc = exc
                    continue
                raise exc

            response_size = len(resp.content)
            retried       = retry_count > 0
            add_attrs([
                ("cds.endpoint",             path),
                ("cds.publisher_id",         publisher_id),
                ("cds.http_status",          resp.status_code),
                ("cds.latency_ms",           latency_ms),
                ("cds.response_size_bytes",  response_size),
                ("cds.retry_count",          retry_count),
                ("cds.retried",              retried),
            ])
            newrelic.agent.record_custom_metric("Custom/CDS/latency_ms",           latency_ms)
            newrelic.agent.record_custom_metric("Custom/CDS/response_size_bytes",  response_size)
            if retried:
                newrelic.agent.record_custom_metric("Custom/CDS/retry_count", 1)
            logger.info(
                "CDS request: path=%s publisher=%s status=%d latency_ms=%.2f size=%d retry=%d",
                path, publisher_id, resp.status_code, latency_ms, response_size, retry_count,
            )
            return resp.json()

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if attempt == 0:
                continue
            break

        except Exception as exc:  # noqa: BLE001 — catch-all to break retry loop on unexpected errors
            last_exc = exc
            break

    # All attempts exhausted
    latency_ms  = round((time.perf_counter() - t0) * 1000, 2)
    http_status = getattr(getattr(last_exc, "response", None), "status_code", None)
    is_timeout  = isinstance(last_exc, requests.exceptions.Timeout) or http_status == 408
    retried     = retry_count > 0
    error_cat   = classify_cds_error(last_exc, http_status)

    if is_timeout:
        add_attrs([("cds.timed_out", True)])
        newrelic.agent.add_custom_span_attribute("cds.timed_out", True)
        newrelic.agent.record_custom_metric("Custom/CDS/timeout_count", 1)
    if retried:
        add_attrs([("cds.retried", True)])
        newrelic.agent.record_custom_metric("Custom/CDS/retry_count", 1)

    notice_err(last_exc, [
        ("error.layer",        "cds"),
        ("error.cds_endpoint", path),
        ("error.http_status",  http_status),
        ("error.retry_count",  retry_count),
        ("error.category",     error_cat),
    ])
    newrelic.agent.record_custom_metric("Custom/CDS/error_count", 1)
    logger.error(
        "CDS request failed: path=%s publisher=%s latency_ms=%.2f http_status=%s "
        "retry=%d timeout=%s category=%s error=%s",
        path, publisher_id, latency_ms, http_status,
        retry_count, is_timeout, error_cat, last_exc, exc_info=True,
    )
    raise last_exc
