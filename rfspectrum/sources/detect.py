"""Find attached SDRs and choose a backend per preset."""

from __future__ import annotations

import logging
from pathlib import Path

from rfspectrum.bands import Preset
from rfspectrum.sources.base import Source

logger = logging.getLogger(__name__)

RTLSDR_IDS = {("0bda", "2838"), ("0bda", "2832")}
HACKRF_IDS = {("1d50", "6089"), ("1d50", "cc15")}  # HackRF One, rad1o


def detect_hardware(sys_usb: str = "/sys/bus/usb/devices") -> dict[str, bool]:
    """Look for RTL-SDR / HackRF USB IDs (Linux sysfs; returns all-False elsewhere)."""
    found = {"rtlsdr": False, "hackrf": False}
    root = Path(sys_usb)
    if not root.is_dir():
        return found
    for dev in root.iterdir():
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if (vid, pid) in RTLSDR_IDS:
            found["rtlsdr"] = True
        if (vid, pid) in HACKRF_IDS:
            found["hackrf"] = True
    return found


def pick_source(preset: Preset, sources: dict[str, Source]) -> Source | None:
    """Prefer RTL-SDR for presets it can tune (frees the HackRF), else HackRF."""
    order = ("sim", "rtlsdr", "rtl_power", "hackrf")
    for name in (*order, *(n for n in sources if n not in order)):
        src = sources.get(name)
        if src is not None and src.supports(preset):
            return src
    return None
