"""rtl_power and hackrf_sweep backends (receive-only command-line sweepers)."""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import time
from typing import Iterator

import numpy as np

from rfspectrum.bands import HACKRF_MAX_HZ, HACKRF_MIN_HZ, RTLSDR_MAX_HZ, RTLSDR_MIN_HZ, Preset
from rfspectrum.frame import Frame
from rfspectrum.grid import resample_max
from rfspectrum.sources.base import Source, StopFn
from rfspectrum.sources.sweep_csv import SweepAssembler

logger = logging.getLogger(__name__)


class _CsvSweepSource(Source):
    binary = ""
    frame_interval_s = 0.1

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def command(self, preset: Preset) -> list[str]:
        raise NotImplementedError

    def frames(self, preset: Preset, should_stop: StopFn) -> Iterator[Frame]:
        cmd = self.command(preset)
        logger.info("Starting %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
        )
        asm = SweepAssembler()
        acc: np.ndarray | None = None
        acc_t0 = time.time()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if should_stop():
                    break
                sweep = asm.feed(line)
                if sweep is None:
                    continue
                row = resample_max(sweep[0], sweep[1], preset)
                # Several fast sweeps per frame: keep the max so short bursts survive.
                acc = row if acc is None else np.maximum(acc, row)
                now = time.time()
                if now - acc_t0 >= self.frame_interval_s:
                    yield Frame(ts=now, preset_id=preset.id, power_db=acc, source=self.name)
                    acc, acc_t0 = None, now
            if proc.poll() is not None and not should_stop():
                logger.warning("%s exited with code %s", self.binary, proc.returncode)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


class RtlPowerSource(_CsvSweepSource):
    """Fallback RTL-SDR backend using the stock `rtl_power` tool (about 1 frame/s)."""

    name, label, binary = "rtl_power", "RTL-SDR (rtl_power)", "rtl_power"

    def __init__(self, gain: str = "auto", ppm: int = 0, device_index: int = 0):
        self.gain, self.ppm, self.device_index = gain, ppm, device_index

    def supports(self, preset: Preset) -> bool:
        return preset.lo_hz >= RTLSDR_MIN_HZ and preset.hi_hz <= RTLSDR_MAX_HZ

    def command(self, preset: Preset) -> list[str]:
        step = max(1, int(preset.bin_hz))
        cmd = [
            self.binary,
            "-f", f"{int(preset.lo_hz)}:{int(preset.hi_hz)}:{step}",
            "-i", "1",
            "-c", "20%",
            "-d", str(self.device_index),
            "-p", str(self.ppm),
        ]
        if self.gain != "auto":
            cmd += ["-g", str(self.gain)]
        return cmd + ["-"]


class HackRfSweepSource(_CsvSweepSource):
    """HackRF One via `hackrf_sweep` (receive-only sweep mode), 1 MHz - 6 GHz."""

    name, label, binary = "hackrf", "HackRF One (hackrf_sweep)", "hackrf_sweep"

    def __init__(self, lna_db: int = 32, vga_db: int = 20, amp: bool = False):
        self.lna_db = int(lna_db) // 8 * 8  # 0-40 in 8 dB steps
        self.vga_db = int(vga_db) // 2 * 2  # 0-62 in 2 dB steps
        self.amp = amp

    def supports(self, preset: Preset) -> bool:
        return preset.lo_hz >= HACKRF_MIN_HZ and preset.hi_hz <= HACKRF_MAX_HZ

    def command(self, preset: Preset) -> list[str]:
        lo = int(math.floor(preset.lo_hz / 1e6))
        hi = int(math.ceil(preset.hi_hz / 1e6))
        if hi - lo < 20:  # hackrf_sweep steps in 20 MHz; pad narrow presets
            mid = (lo + hi) // 2
            lo, hi = mid - 10, mid + 10
        width = int(min(5_000_000, max(2445, preset.bin_hz)))
        return [
            self.binary,
            "-f", f"{lo}:{hi}",
            "-w", str(width),
            "-l", str(self.lna_db),
            "-g", str(self.vga_db),
            "-a", "1" if self.amp else "0",
        ]
