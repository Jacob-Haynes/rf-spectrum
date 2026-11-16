"""Parse the CSV sweep format shared by rtl_power and hackrf_sweep.

Each line: date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, ...
"""

from __future__ import annotations

import numpy as np


def parse_line(line: str) -> tuple[float, float, float, np.ndarray] | None:
    parts = [p.strip() for p in line.strip().split(",")]
    if len(parts) < 7:
        return None
    try:
        lo = float(parts[2])
        hi = float(parts[3])
        step = float(parts[4])
        values = np.array([float(v) for v in parts[6:] if v and v.lower() != "nan"], dtype=np.float64)
    except ValueError:
        return None
    if step <= 0 or values.size == 0:
        return None
    return lo, hi, step, values


class SweepAssembler:
    """Collect CSV segments until the sweep wraps back to its first segment.

    hackrf_sweep emits segments out of order within a sweep (it interleaves
    two 5 MHz halves per tuning step), so a sweep is complete when a segment
    starts at or below the lowest frequency already collected.
    """

    def __init__(self):
        self._freqs: list[np.ndarray] = []
        self._power: list[np.ndarray] = []
        self._min_lo: float | None = None

    def feed(self, line: str) -> tuple[np.ndarray, np.ndarray] | None:
        parsed = parse_line(line)
        if parsed is None:
            return None
        lo, _hi, step, values = parsed
        done = None
        if self._min_lo is not None and lo <= self._min_lo:
            done = self.flush()
        freqs = lo + (np.arange(values.size) + 0.5) * step
        self._freqs.append(freqs)
        self._power.append(values)
        self._min_lo = lo if self._min_lo is None else min(self._min_lo, lo)
        return done

    def flush(self) -> tuple[np.ndarray, np.ndarray] | None:
        if not self._freqs:
            return None
        f = np.concatenate(self._freqs)
        p = np.concatenate(self._power)
        self._freqs, self._power, self._min_lo = [], [], None
        order = np.argsort(f)
        return f[order], p[order]
