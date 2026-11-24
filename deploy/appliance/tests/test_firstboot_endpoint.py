"""Verify the endpoint selection of the public first-boot service."""

from pathlib import Path
import runpy

import pytest


FIRSTBOOT = Path(__file__).resolve().parents[1] / "rootfs-overlay/usr/libexec/rigpilot-firstboot"


def test_firstboot_uses_device_specific_controller(monkeypatch):
    firstboot = runpy.run_path(str(FIRSTBOOT))
    monkeypatch.setenv("RIGPILOT_CONTROLLER_URL", "https://controller.example.invalid/site/")
    assert firstboot["agent_config"]("sample-device")["server_url"] == "https://controller.example.invalid/site"


@pytest.mark.parametrize("url", [None, "http://controller.example.invalid", "https://user@controller.example.invalid", "https://controller.example.invalid/?secret=fixture"])
def test_firstboot_rejects_missing_or_unsafe_controller(monkeypatch, url):
    firstboot = runpy.run_path(str(FIRSTBOOT))
    if url is None:
        monkeypatch.delenv("RIGPILOT_CONTROLLER_URL", raising=False)
    else:
        monkeypatch.setenv("RIGPILOT_CONTROLLER_URL", url)
    with pytest.raises(firstboot["ProvisionError"]):
        firstboot["agent_config"]("sample-device")
