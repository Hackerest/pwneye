import json
import re

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pwneye.config import GITHUB_LATEST_RELEASE_API, VERSION


def _normalize_version(value: str) -> str | None:
    """
    Extract a semantic version from values such as `v1.0.0 [panopticon]`.
    """
    match = re.search(r"\bv?(\d+(?:\.\d+)+)\b", value.strip())
    if match is None:
        return None

    return match.group(1)


def _version_key(value: str) -> tuple[int, ...]:
    """
    Convert a semantic version string into a comparable tuple.
    """
    return tuple(int(part) for part in value.split("."))


def get_latest_release_name(timeout: float = 2.0) -> str | None:
    """
    Return the latest GitHub release title, or None on any failure.
    """
    request = Request(
        GITHUB_LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "pwneye",
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None

    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    return name.strip()

def get_latest_release_version(timeout: float = 2.0) -> str | None:
    """
    Return the normalized version from the latest GitHub release title.
    """
    release_name = get_latest_release_name(timeout=timeout)
    if release_name is None:
        return None

    return _normalize_version(release_name)


def get_available_update(current_version: str = VERSION) -> tuple[str, str] | None:
    """
    Return `(current_version, latest_version)` if a newer release is available.
    """
    latest_version = get_latest_release_version()
    current_normalized = _normalize_version(current_version)

    if latest_version is None or current_normalized is None:
        return None

    if _version_key(latest_version) <= _version_key(current_normalized):
        return None

    return current_normalized, latest_version


def get_update_status(current_version: str = VERSION) -> tuple[str, str | None, bool]:
    """
    Return `(current_version, latest_version_or_none, update_available)`.
    """
    current_normalized = _normalize_version(current_version)
    latest_version = get_latest_release_version()

    if current_normalized is None:
        current_normalized = current_version

    if latest_version is None:
        return current_normalized, None, False

    return (
        current_normalized,
        latest_version,
        _version_key(latest_version) > _version_key(current_normalized),
    )
