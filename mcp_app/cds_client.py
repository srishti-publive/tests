import base64
import logging
import time

import newrelic.agent
import requests

from .nr_utils import add_attrs, notice_err, set_txn_name

logger = logging.getLogger(__name__)

_CDS_BASE = "https://cds-beta.thepublive.com/publisher/{publisher_id}"


def _slugify_path(path: str) -> str:
    slug = path.strip("/").replace("/", "_")
    return slug or "root"


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

    t0 = time.perf_counter()
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Basic {token}"},
            params={k: v for k, v in (params or {}).items() if v is not None and v != ""},
            timeout=30,
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
            raise exc

        response_size = len(resp.content)
        add_attrs([
            ("cds.endpoint", path),
            ("cds.publisher_id", publisher_id),
            ("cds.http_status", resp.status_code),
            ("cds.latency_ms", latency_ms),
            ("cds.response_size_bytes", response_size),
            # NOTE: cds.retry_count and cds.cache_hit removed — they were static
            # placeholders (always 0/False) that carried no signal. Re-add when
            # real retry logic or a cache layer is implemented.
        ])
        # Custom metric for CDS latency — SLO-ready, longer retention than events
        newrelic.agent.record_custom_metric("Custom/CDS/latency_ms", latency_ms)
        newrelic.agent.record_custom_metric("Custom/CDS/response_size_bytes", response_size)

        logger.info(
            "CDS request: path=%s publisher=%s status=%d latency_ms=%.2f size=%d",
            path, publisher_id, resp.status_code, latency_ms, response_size,
        )
        return resp.json()
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        notice_err(exc, [
            ("error.layer", "cds"),
            ("error.cds_endpoint", path),
            ("error.http_status", http_status),
        ])
        newrelic.agent.record_custom_metric("Custom/CDS/error_count", 1)
        logger.error(
            "CDS request failed: path=%s publisher=%s latency_ms=%.2f http_status=%s error=%s",
            path, publisher_id, latency_ms, http_status, exc, exc_info=True,
        )
        raise
