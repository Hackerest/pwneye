import shutil
from pathlib import Path

from typing import List, Tuple, Optional

from pwneye.config import PWNEYE_DIR, CACHE_DIR, RECORDINGS_DIR

def is_first_run() -> bool:
    """
    Returns True if this appears to be the first execution of the tool.
    """
    return not PWNEYE_DIR.exists()

def check_dependencies(dependencies: List[str]) -> Tuple[bool, List[str]]:
    """
    Verifies if the required dependencies are installed on the system.

    :param dependencies: A list of command-line tools to check (e.g., ['ffplay', 'ffmpeg']).
    :return: A tuple where the first element is True if all dependencies are installed,
             and the second element is a list of missing dependencies.
    """
    missing = []
    for dependency in dependencies:
        if shutil.which(dependency) is None:
            missing.append(dependency)

    return len(missing) == 0, missing

def ensure_runtime_dirs() -> tuple[
    Optional[Path],
    Optional[Path],
    Optional[Path],
]:
    """
    Ensure runtime directories exist.

    Returns:
        (
            pwneye_path_if_created,
            cache_path_if_created,
            recordings_path_if_created
        )
    """
    pwneye_created = None
    cache_created = None
    recordings_created = None

    if not PWNEYE_DIR.exists():
        PWNEYE_DIR.mkdir(parents=True, exist_ok=True)
        pwneye_created = PWNEYE_DIR

    if not CACHE_DIR.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_created = CACHE_DIR

    if not RECORDINGS_DIR.exists():
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        recordings_created = RECORDINGS_DIR

    return pwneye_created, cache_created, recordings_created