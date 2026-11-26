"""Supabase Storage over plain REST (no supabase-py: lighter on a 512 MB box).
Every object lives under  <bucket>/<firebase uid>/<name>  so users are isolated."""
import mimetypes

import requests

import config

TIMEOUT = 60


def _headers(extra: dict | None = None) -> dict:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY are not set")
    h = {"apikey": config.SUPABASE_KEY}
    if config.SUPABASE_KEY.startswith("eyJ"):        # legacy JWT service_role key
        h["Authorization"] = f"Bearer {config.SUPABASE_KEY}"
    h.update(extra or {})
    return h


def _api(path: str) -> str:
    return f"{config.SUPABASE_URL}/storage/v1{path}"


def _check(r: requests.Response):
    if not r.ok:
        raise RuntimeError(f"Supabase Storage {r.status_code}: {r.text[:200]}")
    return r


def upload(bucket: str, path: str, data: bytes, content_type: str | None = None) -> None:
    ctype = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
    _check(requests.post(_api(f"/object/{bucket}/{path}"), data=data, timeout=TIMEOUT,
                         headers=_headers({"Content-Type": ctype, "x-upsert": "true"})))


def download(bucket: str, path: str) -> bytes:
    return _check(requests.get(_api(f"/object/authenticated/{bucket}/{path}"),
                               headers=_headers(), timeout=TIMEOUT)).content


def list_files(bucket: str, prefix: str, limit: int = 200) -> list[dict]:
    body = {"prefix": prefix.rstrip("/") + "/", "limit": limit, "offset": 0,
            "sortBy": {"column": "created_at", "order": "asc"}}
    items = _check(requests.post(_api(f"/object/list/{bucket}"), json=body,
                                 headers=_headers(), timeout=TIMEOUT)).json()
    # folders come back with id == null
    return [i for i in items if i.get("id")]


def remove(bucket: str, paths: list[str]) -> None:
    if paths:
        _check(requests.delete(_api(f"/object/{bucket}"), json={"prefixes": paths},
                               headers=_headers(), timeout=TIMEOUT))


def signed_url(bucket: str, path: str, expires: int = 3600) -> str | None:
    try:
        r = _check(requests.post(_api(f"/object/sign/{bucket}/{path}"), json={"expiresIn": expires},
                                 headers=_headers(), timeout=TIMEOUT)).json()
        return config.SUPABASE_URL + "/storage/v1" + r["signedURL"]
    except Exception:
        return None


def ping() -> bool:
    """Cheap call used by the cron tick so a free Supabase project never idles into a pause."""
    try:
        requests.post(_api(f"/object/list/{config.SCREENSHOT_BUCKET}"),
                      json={"prefix": "", "limit": 1}, headers=_headers(), timeout=20)
        return True
    except Exception:
        return False
