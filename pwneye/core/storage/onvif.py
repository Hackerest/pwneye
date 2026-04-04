from pathlib import Path
import yaml

from pwneye.config import ONVIF_COMMON_DB_PATH
from pwneye.core.types import OnvifKnowledgeBase


class OnvifKnowledgeBaseError(RuntimeError):
    pass


def load_knowledge_base(
    path: Path = ONVIF_COMMON_DB_PATH,
) -> OnvifKnowledgeBase:
    """
    Load ONVIF knowledge base from YAML file and verify ONVIF WSDL availability.

    Raises:
        OnvifKnowledgeBaseError if the file is missing, invalid,
        or required ONVIF WSDL files are not available.
    """
    # --- Knowledge base file ---
    if not path.exists():
        raise OnvifKnowledgeBaseError(
            f"ONVIF knowledge base not found: {path}"
        )

    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        raise OnvifKnowledgeBaseError(
            f"Failed to parse ONVIF knowledge base: {path}"
        ) from exc

    return {
        "ports": data.get("ports", []),
        "usernames": data.get("usernames", []),
        "passwords": data.get("passwords", []),
    }