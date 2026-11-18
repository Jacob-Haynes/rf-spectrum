"""Primary RTL-SDR backend: pyrtlsdr + numpy FFT, hopping across wide presets.

Typical rates on a Pi 5: single-hop presets (PMR446, 433) ~8-10 frames/s,
868 MHz (4 hops) ~5-8 frames/s, 26-30 MHz presets ~1-2 frames/s.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Iterator

import numpy as np

from rfspectrum.bands import RTLSDR_MAX_HZ, RTLSDR_MIN_HZ, Preset
from rfspectrum.frame import Frame
from rfspectrum.grid import db, resample_max
from rfspectrum.sources.base import Source, StopFn

logger = logging.getLogger(__name__)

SAMPLE_RATE = 2.4e6
USABLE = 0.8  # use the flat middle 80% of each hop


def hop_centres(preset: Preset, sample_rate: float = SAMPLE_RATE) -> list[float]:
    usable = sample_rate * USABLE
    if preset.span_hz <= usable:
        return [(preset.lo_hz + preset.hi_hz) / 2]
    n = math.ceil(preset.span_hz / usable)
    step = preset.span_hz / n
    return [preset.lo_hz + step * (i + 0.5) for i in range(n)]


def fft_size(preset: Preset, sample_rate: float = SAMPLE_RATE) -> int:
    want = sample_rate / max(preset.bin_hz / 2, 1.0)
    n = 1 << max(8, math.ceil(math.log2(want)))
    return int(min(n, 16384))


def psd_db(samples: np.ndarray, nfft: int) -> np.ndarray:
    """Welch-style averaged power spectrum (fftshifted), DC spike removed."""
    samples = samples - np.mean(samples)
    nseg = max(1, samples.size // nfft)
    seg = samples[: nseg * nfft].reshape(nseg, nfft) * np.hanning(nfft)
    spec = np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0) / nfft
    spec = np.fft.fftshift(spec)
    c = nfft // 2
    spec[c - 1 : c + 2] = np.median(spec[c - 6 : c + 6])
    return db(spec)


class RtlSdrSource(Source):
    name, label = "rtlsdr", "RTL-SDR (pyrtlsdr)"

    def __init__(self, gain: str = "auto", ppm: int = 0, device_index: int = 0, averages: int = 24):
        self.gain = gain
        self.ppm = ppm
        self.device_index = device_index
        self.averages = averages
        self._sdr = None

    @staticmethod
    def available() -> bool:
        try:
            __import__("rtlsdr")  # loads librtlsdr via ctypes; fails if missing
        except Exception:
            return False
        return True

    def supports(self, preset: Preset) -> bool:
        return preset.lo_hz >= RTLSDR_MIN_HZ and preset.hi_hz <= RTLSDR_MAX_HZ

    def _open(self):
        if self._sdr is None:
            from rtlsdr import RtlSdr

            sdr = RtlSdr(device_index=self.device_index)
            sdr.sample_rate = SAMPLE_RATE
            if self.ppm:
                sdr.freq_correction = self.ppm
            sdr.gain = "auto" if self.gain == "auto" else float(self.gain)
            self._sdr = sdr
        return self._sdr

    def frames(self, preset: Preset, should_stop: StopFn) -> Iterator[Frame]:
        sdr = self._open()
        centres = hop_centres(preset)
        nfft = fft_size(preset)
        half = SAMPLE_RATE * (USABLE / 2 if len(centres) > 1 else 0.5)
        offsets = (np.arange(nfft) - nfft / 2) * (SAMPLE_RATE / nfft)
        while not should_stop():
            freqs, power = [], []
            for fc in centres:
                sdr.center_freq = fc
                sdr.read_samples(8192)  # let the tuner/AGC settle, discard
                p = psd_db(sdr.read_samples(nfft * self.averages), nfft)
                keep = np.abs(offsets) <= half
                freqs.append(fc + offsets[keep])
                power.append(p[keep])
            row = resample_max(np.concatenate(freqs), np.concatenate(power), preset)
            yield Frame(ts=time.time(), preset_id=preset.id, power_db=row, source=self.name)

    def close(self) -> None:
        if self._sdr is not None:
            try:
                self._sdr.close()
            except Exception:
                pass
            self._sdr = None
