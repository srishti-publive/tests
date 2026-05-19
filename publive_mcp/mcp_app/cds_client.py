import base64
import requests

_CDS_BASE = "https://cds.thepublive.com/publisher/{publisher_id}"


def cds_get(credentials, path, params=None):
    publisher_id = credentials["publisherId"]
    api_key      = credentials["apiKey"]
    api_secret   = credentials["apiSecret"]

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
        raise Exception(msg)

    return resp.json()
