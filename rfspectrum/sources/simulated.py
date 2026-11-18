from __future__ import annotations

import time
from typing import Iterator

from rfspectrum.bands import Preset
from rfspectrum.frame import Frame
from rfspectrum.simulator import SimulatedEnvironment
from rfspectrum.sources.base import Source, StopFn


class SimulatedSource(Source):
    name, label, simulated = "sim", "SIMULATED (no SDR)", True

    def __init__(self, env: SimulatedEnvironment, frame_s: float = 0.1):
        self.env = env
        self.frame_s = frame_s

    def supports(self, preset: Preset) -> bool:
        return True

    def frames(self, preset: Preset, should_stop: StopFn) -> Iterator[Frame]:
        next_t = time.time()
        while not should_stop():
            t0 = time.time()
            power = self.env.render(preset, t0, self.frame_s)
            yield Frame(ts=t0, preset_id=preset.id, power_db=power, source=self.name)
            next_t += self.frame_s
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.time()
