from pathlib import Path
import yaml

from pwneye.config import RTSP_COMMON_DB_PATH
from pwneye.core.types import RtspKnowledgeBase


class RtspKnowledgeBaseError(RuntimeError):
    pass


def load_knowledge_base(
    path: Path = RTSP_COMMON_DB_PATH,
) -> RtspKnowledgeBase:
    """
    Load RTSP knowledge base from YAML file.

    Raises:
        RtspKnowledgeBaseError if the file is missing or invalid.
    """
    if not path.exists():
        raise RtspKnowledgeBaseError(
            f"RTSP knowledge base not found: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise RtspKnowledgeBaseError(
            f"Failed to parse RTSP knowledge base: {path}"
        ) from exc

    # Normalize base fields
    ports = data.get("ports", [])
    common_paths = data.get("common_paths", [])
    common_creds = data.get("common_creds", {})
    vendors = data.get("vendors", [])

    # Defensive normalization
    normalized_vendors = []
    for v in vendors:
        normalized_vendors.append({
            "name": v.get("name", ""),
            "banners": v.get("banners", []),
            "paths": {
                "tcp": v.get("paths", {}).get("tcp", []),
                "udp": v.get("paths", {}).get("udp", []),
            },
            "creds": {
                "usernames": v.get("creds", {}).get("usernames", []),
                "passwords": v.get("creds", {}).get("passwords", []),
            },
        })

    return {
        "ports": ports,
        "common_paths": common_paths,
        "common_creds": {
            "usernames": common_creds.get("usernames", []),
            "passwords": common_creds.get("passwords", []),
        },
        "vendors": normalized_vendors,
    }

def _normalize_vendor_name(value: str) -> str:
    """
    Normalize vendor names for loose matching.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


def find_vendor_matches(vendor: str | None, kb: RtspKnowledgeBase) -> list[dict]:
    """
    Return knowledge base entries matching the given vendor name.
    """
    if not vendor:
        return []

    vendor_lower = vendor.lower().strip()
    vendor_normalized = _normalize_vendor_name(vendor)

    exact_matches = []
    normalized_matches = []

    for entry in kb.get("vendors", []):
        name = entry.get("name") or ""
        if not name:
            continue

        name_lower = name.lower()
        if name_lower == vendor_lower:
            exact_matches.append(entry)
            continue

        if _normalize_vendor_name(name) == vendor_normalized:
            normalized_matches.append(entry)

    return exact_matches or normalized_matches


def find_vendor_entry(vendor: str | None, kb: RtspKnowledgeBase) -> dict | None:
    """
    Return the RTSP knowledge base entry matching the given vendor name.
    """
    matches = find_vendor_matches(vendor, kb)
    if not matches:
        return None

    return matches[0]

def identify_vendor_from_banner(
    banner: str | None,
    kb: RtspKnowledgeBase,
) -> str | None:
    """
    Infer the vendor name from a previously captured RTSP banner.
    """
    if not banner:
        return None

    banner = banner.lower()

    for entry in kb.get("vendors", []):
        for marker in entry.get("banners", []):
            if marker.lower() in banner:
                return entry.get("name")

    return None

def is_vendor_in_db(vendor: str, kb: RtspKnowledgeBase) -> bool:
    return find_vendor_entry(vendor, kb) is not None

def get_all_vendors(kb: RtspKnowledgeBase) -> list[str]:
    return sorted(
        {
            entry["name"]
            for entry in kb.get("vendors", [])
            if "name" in entry
        }
    )

def get_all_paths(
    kb: RtspKnowledgeBase,
    protocol: str,
) -> list[str]:
    """
    Return all RTSP paths from the knowledge base for the given protocol.
    """
    paths = list(kb.get("common_paths", []))

    for entry in kb.get("vendors", []):
        paths.extend(entry.get("paths", {}).get(protocol, []))

    return paths
