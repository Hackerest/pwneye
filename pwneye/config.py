from pathlib import Path

# ------------------------------------------
# Tool metadata
# ------------------------------------------

DEVELOPER = "Michele 'robo7nik' Cisternino"
VERSION = "1.1.0"
CODENAME = "panopticon"
REPO = "https://github.com/hackerest/pwneye"
GITHUB_LATEST_RELEASE_API = "https://api.github.com/repos/Hackerest/pwneye/releases/latest"

# ------------------------------------------
# Package Paths
# ------------------------------------------

# Base directory of the pwneye package
BASE_DIR = Path(__file__).resolve().parent

# Data directory (read-only, shipped with the tool)
DATA_DIR = BASE_DIR / "data"

# ------------------------------------------
# User directories (~/.pwneye)
# ------------------------------------------

HOME_DIR = Path.home()
PWNEYE_DIR = HOME_DIR / ".pwneye"
CACHE_DIR = PWNEYE_DIR / "cache"
RECORDINGS_DIR = PWNEYE_DIR / "recordings"
SNAPSHOTS_DIR = PWNEYE_DIR / "snapshots"

# ------------------------------------------
# ONVIF
# ------------------------------------------

ONVIF_COMMON_DB_PATH = DATA_DIR / "onvif_common.yaml"
MOTD_DB_PATH = DATA_DIR / "motd.yaml"

# ------------------------------------------
# RTSP
# ------------------------------------------

RTSP_COMMON_DB_PATH = DATA_DIR / "rtsp.yaml"
