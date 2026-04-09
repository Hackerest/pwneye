from datetime import datetime, timezone
from pathlib import Path
import re
import yaml

from pwneye.config import CACHE_DIR


def _utc_now() -> str:
    """
    Return the current UTC time in ISO 8601 format.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cache_path(host: str) -> Path:
    """
    Build the cache file path for a target host.
    """
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return CACHE_DIR / f"{safe_host}.yaml"


def _empty_document(host: str) -> dict:
    """
    Create a new cache document for the given target.
    """
    now = _utc_now()
    return {
        "target": {
            "host": host,
            "first_seen": now,
            "last_seen": now,
        },
        "onvif": {},
        "rtsp": {},
    }


def load_target(host: str) -> dict | None:
    """
    Load a target cache entry if it exists.
    """
    path = _cache_path(host)
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return None

    target = data.setdefault("target", {})
    target.setdefault("host", host)
    target.setdefault("first_seen", _utc_now())
    target["last_seen"] = _utc_now()

    data.setdefault("onvif", {})
    data.setdefault("rtsp", {})
    return data


def save_target(host: str, data: dict) -> None:
    """
    Save a target cache entry to disk.
    """
    path = _cache_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)

    target = data.setdefault("target", {})
    target.setdefault("host", host)
    target.setdefault("first_seen", _utc_now())
    target["last_seen"] = _utc_now()

    data.setdefault("onvif", {})
    data.setdefault("rtsp", {})

    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            data,
            handle,
            sort_keys=False,
            allow_unicode=False,
        )


def upsert_onvif_success(
    host: str,
    *,
    port: int,
    username: str,
    password: str,
    manufacturer: str | None = None,
    streams: list[str] | None = None,
) -> None:
    """
    Persist a successful ONVIF authentication for the target.
    """
    data = load_target(host) or _empty_document(host)
    onvif = data.setdefault("onvif", {})

    onvif.update({
        "supported": True,
        "port": port,
        "auth": {
            "username": username,
            "password": password,
        },
    })

    if manufacturer:
        onvif["manufacturer"] = manufacturer

    if streams:
        onvif["streams"] = list(dict.fromkeys(streams))

    save_target(host, data)


def upsert_onvif_discovery(
    host: str,
    *,
    manufacturer: str | None = None,
) -> None:
    """
    Persist non-authenticated ONVIF discovery data for the target.
    """
    if not manufacturer:
        return

    data = load_target(host) or _empty_document(host)
    onvif = data.setdefault("onvif", {})
    onvif["manufacturer"] = manufacturer
    save_target(host, data)


def upsert_rtsp_banner(
    host: str,
    *,
    port: int,
    banner: str,
) -> None:
    """
    Persist an RTSP banner for the target.
    """
    if not banner:
        return

    data = load_target(host) or _empty_document(host)
    rtsp = data.setdefault("rtsp", {})
    rtsp["banner"] = {
        "port": port,
        "value": banner,
    }
    save_target(host, data)


def upsert_rtsp_success(
    host: str,
    *,
    port: int,
    username: str,
    password: str,
    path: str,
    protocol: str,
    url: str,
) -> None:
    """
    Persist a successful RTSP authentication for the target.
    """
    data = load_target(host) or _empty_document(host)
    rtsp = data.setdefault("rtsp", {})

    rtsp.update({
        "supported": True,
        "port": port,
        "auth": {
            "username": username,
            "password": password,
        },
        "path": path,
        "protocol": protocol,
        "url": url,
    })

    save_target(host, data)


def get_cached_onvif_auth(data: dict | None) -> dict | None:
    """
    Return cached ONVIF authentication details, if available.
    """
    if not data:
        return None

    onvif = data.get("onvif") or {}
    auth = onvif.get("auth") or {}

    username = auth.get("username")
    password = auth.get("password")
    port = onvif.get("port")

    if port is None or username is None or password is None:
        return None

    return {
        "port": port,
        "username": username,
        "password": password,
        "manufacturer": onvif.get("manufacturer"),
        "streams": onvif.get("streams", []),
    }


def get_cached_onvif_manufacturer(data: dict | None) -> str | None:
    """
    Return a cached ONVIF manufacturer hint, if available.
    """
    if not data:
        return None

    onvif = data.get("onvif") or {}
    manufacturer = onvif.get("manufacturer")
    if not manufacturer:
        return None

    return str(manufacturer)


def get_cached_rtsp_banner(data: dict | None) -> dict | None:
    """
    Return a cached RTSP banner, if available.
    """
    if not data:
        return None

    rtsp = data.get("rtsp") or {}
    banner = rtsp.get("banner") or {}
    port = banner.get("port")
    value = banner.get("value")
    if port is None or not value:
        return None

    return {
        "port": port,
        "value": str(value),
    }


def get_cached_rtsp_auth(data: dict | None) -> dict | None:
    """
    Return cached RTSP authentication details, if available.
    """
    if not data:
        return None

    rtsp = data.get("rtsp") or {}
    auth = rtsp.get("auth") or {}

    username = auth.get("username")
    password = auth.get("password")
    port = rtsp.get("port")
    path = rtsp.get("path")
    protocol = rtsp.get("protocol")
    url = rtsp.get("url")

    if None in (port, path, protocol, url, username, password):
        return None

    return {
        "port": port,
        "path": path,
        "protocol": protocol,
        "url": url,
        "username": username,
        "password": password,
    }
