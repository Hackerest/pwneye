import random
from pathlib import Path

import yaml

from pwneye.config import MOTD_DB_PATH


class MotdError(RuntimeError):
    pass


def load_messages(path: Path = MOTD_DB_PATH) -> list[str]:
    """
    Load MOTD messages from YAML.

    Raises:
        MotdError if the file is missing or invalid.
    """
    if not path.exists():
        raise MotdError(f"MOTD file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:
        raise MotdError(f"Failed to parse MOTD file: {path}") from exc

    messages = data.get("messages", [])
    if not isinstance(messages, list):
        raise MotdError("Invalid MOTD format: 'messages' must be a list")

    return [
        str(message).strip()
        for message in messages
        if str(message).strip()
    ]


def get_random_message(path: Path = MOTD_DB_PATH) -> str | None:
    """
    Return a random MOTD message, or None if no message is available.
    """
    messages = load_messages(path)
    if not messages:
        return None

    return random.choice(messages)
