from pathlib import Path

# ------------------------------------------
# Tool metadata
# ------------------------------------------

DEVELOPER = "robo7nik"
VERSION = "1.0.2"
CODENAME = "panopticon"
REPO = "https://github.com/hackerest/pwneye"

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

# ------------------------------------------
# ONVIF
# ------------------------------------------

ONVIF_COMMON_DB_PATH = DATA_DIR / "onvif_common.yaml"

# ------------------------------------------
# RTSP
# ------------------------------------------

RTSP_COMMON_DB_PATH = DATA_DIR / "rtsp.yaml"
