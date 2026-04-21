from enum import IntEnum

from typing import Protocol, Sequence, TypeVar, Optional, Iterable, Mapping, Any
from typing import TypedDict, List, Dict

from dataclasses import dataclass

T = TypeVar("T")

class ExitCode(IntEnum):
    SUCCESS = 0
    FAILURE = 1
    USER_ABORT = 2

class PromptInterrupt(KeyboardInterrupt):
    """
    Raised when CTRL-C is pressed while waiting for interactive input.
    """

@dataclass
class Result:
    ok: bool
    exit_code: ExitCode

@dataclass(frozen=True)
class RtspAttempt:
    host: str
    port: int
    path: str
    username: str
    password: str
    protocol: str
    url: str

@dataclass(frozen=True)
class RtspProbeResult:
    url: str
    status_code: Optional[int]
    reason: str
    auth_scheme: Optional[str]
    credentials_valid: bool
    path_valid: bool
    stream_available: bool
    error: Optional[str] = None

class TUI(Protocol):
    # Basic logging
    def info(self, message: str) -> None: ...
    def info2(self, message: str) -> None: ...
    def success(self, message: str) -> None: ...
    def warning(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...
    def debug(self, message: str) -> None: ...

    # Blocks / formatting
    def block(self, content: Iterable[str] | Mapping[str, Any], indent: int = 3) -> None: ...
    def start_live(self, message: str, spinner: str = "dots") -> None: ...
    def update_live(self, message: str) -> None: ...
    def stop_live(self) -> None: ...

    # Prompts
    def select(
        self,
        prompt: str,
        options: Sequence[T],
    ) -> T: ...

    def confirm(self, prompt: str, default: bool = True, interrupt_message: str | None = "CTRL-C detected. Aborting execution...") -> bool: ...

class OnvifKnowledgeBase(TypedDict):
    ports: List[int]
    usernames: List[str]
    passwords: List[str]

class RtspVendor(TypedDict):
    name: str
    banners: List[str]
    paths: Dict[str, List[str]]  # {"tcp": [...], "udp": [...]}
    creds: Dict[str, List[str]]  # {"usernames": [...], "passwords": [...]}

class RtspKnowledgeBase(TypedDict):
    ports: List[int]
    common_paths: List[str]
    common_creds: Dict[str, List[str]]
    vendors: List[RtspVendor]
