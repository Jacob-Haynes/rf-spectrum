"""Map arbitrary (freq, power) measurements onto a preset's fixed display grid."""

from __future__ import annotations

import numpy as np

from rfspectrum.bands import Preset


def resample_max(freqs_hz: np.ndarray, power_db: np.ndarray, preset: Preset) -> np.ndarray:
    """Max-pool measurements into the preset's bins.

    Max pooling keeps narrow signals visible when the source resolution is finer
    than the display. Display bins that receive no measurement (source coarser
    than display) are filled by linear interpolation.
    """
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    power_db = np.asarray(power_db, dtype=np.float64)
    out = np.full(preset.bins, np.nan)
    if freqs_hz.size == 0:
        return np.full(preset.bins, -120.0, dtype=np.float32)

    idx = np.floor((freqs_hz - preset.lo_hz) / preset.bin_hz).astype(np.int64)
    ok = (idx >= 0) & (idx < preset.bins) & np.isfinite(power_db)
    if ok.any():
        np.fmax.at(out, idx[ok], power_db[ok])

    missing = np.isnan(out)
    if missing.all():
        # Nothing landed inside the grid: interpolate from what we have.
        order = np.argsort(freqs_hz)
        out = np.interp(preset.freqs_hz(), freqs_hz[order], power_db[order])
    elif missing.any():
        have = np.flatnonzero(~missing)
        out[missing] = np.interp(np.flatnonzero(missing), have, out[have])
    return out.astype(np.float32)


def db(x: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(np.maximum(x, 1e-20))
