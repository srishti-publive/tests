import base64
import logging
import time

import newrelic.agent
import requests

from .nr_utils import add_attrs, notice_err, set_txn_name

logger = logging.getLogger(__name__)

_CDS_BASE = "https://cds-beta.thepublive.com/publisher/{publisher_id}"

# Client-side timeout per attempt (seconds).  Must be well under any AI-client
# timeout so we can retry and still return a useful error rather than hanging.
_REQUEST_TIMEOUT = 5

# How long to wait between the first attempt and the single retry (seconds).
_RETRY_BACKOFF = 1


def _slugify_path(path: str) -> str:
    slug = path.strip("/").replace("/", "_")
    return slug or "root"


def _is_retryable(exc) -> bool:
    """True for transient failures worth retrying once: timeouts and HTTP 408."""
    if isinstance(exc, requests.exceptions.Timeout):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 408


def _cds_error_category(exc, http_status) -> str:
    """Classify a CDS failure for the error.category transaction attribute.

    Categories (used consistently across all layers):
      timeout       — requests.Timeout or HTTP 408
      auth_error    — HTTP 401 (bad / expired credentials)
      client_error  — HTTP 4xx other than 401/408 (bad request from our side)
      upstream_error — HTTP 5xx (CDS internal failure)
      system_error  — anything else (network error, unexpected exception)
    """
    if isinstance(exc, requests.exceptions.Timeout) or http_status == 408:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status and 400 <= http_status < 500:
        return "client_error"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"


@newrelic.agent.function_trace(name="cds_get", group="CDS")
def cds_get(credentials, path, params=None):
    set_txn_name(f"CDS/{_slugify_path(path)}", group="CDS")

    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")

    api_key    = credentials.get("apiKey", "")
    api_secret = credentials.get("apiSecret", "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    url   = _CDS_BASE.format(publisher_id=publisher_id) + path

    # Span-level attributes for distributed tracing — visible in NR trace waterfall
    # even when the CDS service is on a separate entity.
    newrelic.agent.add_custom_span_attribute("cds.url", url)
    newrelic.agent.add_custom_span_attribute("cds.publisher_id", publisher_id)
    newrelic.agent.add_custom_span_attribute("cds.path", path)

    clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}

    t0 = time.perf_counter()
    last_exc = None
    retry_count = 0

    for attempt in range(2):  # attempt 0 = first try, attempt 1 = single retry
        if attempt > 0:
            time.sleep(_RETRY_BACKOFF)
            retry_count = attempt
            logger.warning(
                "CDS retry attempt %d: path=%s publisher=%s",
                attempt, path, publisher_id,
            )

        try:
            resp = requests.get(
                url,
                headers={"Authorization": f"Basic {token}"},
                params=clean_params,
                timeout=_REQUEST_TIMEOUT,
            )

            latency_ms = round((time.perf_counter() - t0) * 1000, 2)

            if not resp.ok:
                try:
                    data = resp.json()
                    msg = data.get("detail") or data.get("message") or f"HTTP {resp.status_code}"
                except Exception:
                    msg = f"HTTP {resp.status_code}"
                exc = Exception(f"{msg} [url={url}]")
                exc.response = resp  # type: ignore[attr-defined]

                # 408 is retryable — save and loop; anything else surfaces immediately
                if resp.status_code == 408 and attempt == 0:
                    last_exc = exc
                    continue
                raise exc

            response_size = len(resp.content)
            retried = retry_count > 0
            add_attrs([
                ("cds.endpoint", path),
                ("cds.publisher_id", publisher_id),
                ("cds.http_status", resp.status_code),
                ("cds.latency_ms", latency_ms),
                ("cds.response_size_bytes", response_size),
                ("cds.retry_count", retry_count),
                ("cds.retried", retried),
            ])
            # Custom metric for CDS latency — SLO-ready, longer retention than events
            newrelic.agent.record_custom_metric("Custom/CDS/latency_ms", latency_ms)
            newrelic.agent.record_custom_metric("Custom/CDS/response_size_bytes", response_size)
            if retried:
                # Count retries that eventually succeeded (useful for retry effectiveness %)
                newrelic.agent.record_custom_metric("Custom/CDS/retry_count", 1)

            logger.info(
                "CDS request: path=%s publisher=%s status=%d latency_ms=%.2f size=%d retry=%d",
                path, publisher_id, resp.status_code, latency_ms, response_size, retry_count,
            )
            return resp.json()

        except requests.exceptions.Timeout as exc:
            last_exc = exc
            if attempt == 0:
                continue  # retry once
            # Both attempts timed out — fall through to error handling below
            break

        except Exception as exc:
            last_exc = exc
            break  # non-retryable — surface immediately

    # ── All attempts exhausted ────────────────────────────────────────────────
    latency_ms = round((time.perf_counter() - t0) * 1000, 2)
    http_status = getattr(getattr(last_exc, "response", None), "status_code", None)
    is_timeout = isinstance(last_exc, requests.exceptions.Timeout) or http_status == 408
    retried = retry_count > 0
    error_category = _cds_error_category(last_exc, http_status)

    # Set timeout and retry flags as transaction + span attributes for NRQL filtering
    if is_timeout:
        add_attrs([("cds.timed_out", True)])
        newrelic.agent.add_custom_span_attribute("cds.timed_out", True)
        newrelic.agent.record_custom_metric("Custom/CDS/timeout_count", 1)
    if retried:
        add_attrs([("cds.retried", True)])
        newrelic.agent.record_custom_metric("Custom/CDS/retry_count", 1)

    notice_err(last_exc, [
        ("error.layer", "cds"),
        ("error.cds_endpoint", path),
        ("error.http_status", http_status),
        ("error.retry_count", retry_count),
        ("error.category", error_category),
    ])
    newrelic.agent.record_custom_metric("Custom/CDS/error_count", 1)
    logger.error(
        "CDS request failed: path=%s publisher=%s latency_ms=%.2f http_status=%s "
        "retry=%d timeout=%s category=%s error=%s",
        path, publisher_id, latency_ms, http_status,
        retry_count, is_timeout, error_category, last_exc, exc_info=True,
    )
    raise last_exc
