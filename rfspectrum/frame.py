from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    """One spectrum measurement on a preset's display grid."""

    ts: float  # epoch seconds
    preset_id: str
    power_db: np.ndarray  # shape (preset.bins,), float32, uncalibrated dB
    source: str = ""
