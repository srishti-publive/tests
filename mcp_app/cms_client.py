import base64
import logging

import requests

logger = logging.getLogger(__name__)

_CMS_BASE = "https://cms.thepublive.com/publisher/{publisher_id}"
_REQUEST_TIMEOUT = 10


# ── Auth & URL helpers ────────────────────────────────────────────────────────

def _base_url(credentials: dict) -> str:
    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")
    return _CMS_BASE.format(publisher_id=publisher_id)


def _auth_headers(credentials: dict) -> dict:
    api_key    = credentials.get("apiKey", "")
    api_secret = credentials.get("apiSecret", "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }


# ── Error normalisation ───────────────────────────────────────────────────────

def _handle_error(exc, url: str) -> dict:
    """Convert an HTTP exception into a structured error dict.

    All CMS tools return this shape on failure so the AI client can decide
    whether to retry or surface a human-readable message.

    error_type values:
      auth_error      — HTTP 401, bad / expired credentials
      not_found       — HTTP 404, resource does not exist
      bad_request     — HTTP 400-499 (other), caller sent invalid data
      upstream_error  — HTTP 5xx, CMS server failure
      system_error    — anything else (network, unexpected)
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

def cms_get(credentials: dict, path: str, params: dict | None = None) -> dict:
    """GET request to the CMS API — used for list and retrieve operations."""
    url = _base_url(credentials) + path
    clean_params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        resp = requests.get(
            url,
            headers=_auth_headers(credentials),
            params=clean_params,
            timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp  # type: ignore[attr-defined]
            return _handle_error(exc, url)
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        logger.error("cms_get: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


def cms_post(credentials: dict, path: str, body: dict) -> dict:
    """POST request to the CMS API — used for create operations."""
    url = _base_url(credentials) + path
    try:
        resp = requests.post(
            url,
            headers=_auth_headers(credentials),
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp  # type: ignore[attr-defined]
            return _handle_error(exc, url)
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        logger.error("cms_post: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


def cms_patch(credentials: dict, path: str, body: dict) -> dict:
    """PATCH request to the CMS API — used for update operations."""
    url = _base_url(credentials) + path
    try:
        resp = requests.patch(
            url,
            headers=_auth_headers(credentials),
            json=body,
            timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp  # type: ignore[attr-defined]
            return _handle_error(exc, url)
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        logger.error("cms_patch: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise


def cms_delete(credentials: dict, path: str) -> dict:
    """DELETE request to the CMS API — used for delete operations."""
    url = _base_url(credentials) + path
    try:
        resp = requests.delete(
            url,
            headers=_auth_headers(credentials),
            timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            exc = Exception(f"HTTP {resp.status_code}")
            exc.response = resp  # type: ignore[attr-defined]
            return _handle_error(exc, url)
        return resp.json()
    except requests.exceptions.Timeout:
        return {"error_type": "timeout", "message": "CMS request timed out.", "retryable": True}
    except requests.exceptions.ConnectionError:
        return {"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}
    except Exception as exc:
        logger.error("cms_delete: unexpected error: path=%s error=%s", path, exc, exc_info=True)
        raise
