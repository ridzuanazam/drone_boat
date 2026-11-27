"""Configuration settings for the Raspberry Pi 5 Drone Boat server."""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
PHOTOS_DIR = os.path.join(MEDIA_DIR, "photos")
VIDEOS_DIR = os.path.join(MEDIA_DIR, "videos")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "5000"))

EV3_HOST = os.environ.get("EV3_HOST", "ev3dev.local")
EV3_PORT = int(os.environ.get("EV3_PORT", "5555"))
EV3_DISCOVERY_PORT = int(os.environ.get("EV3_DISCOVERY_PORT", "5556"))

MAX_SPEED = int(os.environ.get("MAX_SPEED", "900"))
COMMAND_TIMEOUT_S = float(os.environ.get("COMMAND_TIMEOUT_S", "1.2"))

CAM_WIDTH = int(os.environ.get("CAM_WIDTH", "960"))
CAM_HEIGHT = int(os.environ.get("CAM_HEIGHT", "540"))
CAM_FPS = int(os.environ.get("CAM_FPS", "25"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "80"))

AI_MODEL_PATH = os.environ.get(
    "AI_MODEL_PATH",
    "/usr/share/imx500-models/imx500_network_ssd_mobilenetv2_fpnlite_320x320_pp.rpk",
)
AI_THRESHOLD = float(os.environ.get("AI_THRESHOLD", "0.5"))
AI_ENABLED_DEFAULT = os.environ.get("AI_ENABLED", "1") == "1"


def ensure_media_dirs():
    """Create photo and video output folders if they do not exist."""
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    os.makedirs(VIDEOS_DIR, exist_ok=True)
