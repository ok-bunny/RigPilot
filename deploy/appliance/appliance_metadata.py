"""Trusted build metadata shared by image tools; never reads an inspected image."""
from __future__ import annotations
import json
from pathlib import Path

PROFILE_PATH = Path(__file__).with_name("build-profile.json")
_PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
IMAGE_RELEASE = _PROFILE["next_release"]
NVIDIA_DRIVER_VERSION = _PROFILE["nvidia"]["driver_version"]

if __name__ == "__main__":
    print(IMAGE_RELEASE)
