from __future__ import annotations

from typing import Callable, Iterator

from rfspectrum.bands import Preset
from rfspectrum.frame import Frame

StopFn = Callable[[], bool]


class Source:
    """A receiver that produces Frames for one preset until told to stop."""

    name = "base"
    label = "base"
    simulated = False

    def supports(self, preset: Preset) -> bool:
        raise NotImplementedError

    def frames(self, preset: Preset, should_stop: StopFn) -> Iterator[Frame]:
        raise NotImplementedError

    def close(self) -> None:
        pass
