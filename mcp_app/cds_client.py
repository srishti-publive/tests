import base64
import requests

_CDS_BASE = "https://cds-beta.thepublive.com/publisher/{publisher_id}"


def cds_get(credentials, path, params=None):
    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")

    api_key    = credentials.get("apiKey", "")
    api_secret = credentials.get("apiSecret", "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    url   = _CDS_BASE.format(publisher_id=publisher_id) + path

    resp = requests.get(
        url,
        headers={"Authorization": f"Basic {token}"},
        params={k: v for k, v in (params or {}).items() if v is not None and v != ""},
        timeout=30,
    )

    if not resp.ok:
        try:
            data = resp.json()
            msg = data.get("detail") or data.get("message") or f"HTTP {resp.status_code}"
        except Exception:
            msg = f"HTTP {resp.status_code}"
        raise Exception(f"{msg} [url={url}]")

    return resp.json()
