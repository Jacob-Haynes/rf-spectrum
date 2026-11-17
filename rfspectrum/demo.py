"""Presenter-triggered demo scenarios (demo trigger contract v6).

Each scenario switches on one or more simulated emitters for a fixed time. They exist only in
simulate mode: with a real SDR attached there is nothing to inject into, and the real receiver
keeps running untouched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    hint: str
    duration_s: float
    emitters: tuple[str, ...]
    preset: str | None = None  # band to show, unless drone watch is sweeping
    drone: bool = False  # starts the 2.4 / 5.8 GHz drone watch

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "hint": self.hint, "duration_s": self.duration_s}


SCENARIOS: dict[str, Scenario] = {s.id: s for s in (
    Scenario("fpv-flight", "FPV drone in flight", "ELRS 2.4 GHz control and DJI digital video on 5.8 GHz",
             90, ("drone_flight",), drone=True),
    Scenario("fpv-power-on", "FPV drone powered up", "Control link and idle video before take-off",
             60, ("drone_power",), drone=True),
    Scenario("meshtastic", "Meshtastic node", "LoRa mesh bursts on 869.5 MHz", 60, ("meshtastic",), "868"),
    Scenario("elrs-868", "ELRS 868 control link", "Long-range RC link hopping across 863-870 MHz",
             60, ("elrs868",), "868"),
    Scenario("phone", "Smartphone", "LTE 800 uplink and Wi-Fi bursts", 60, ("phone",), "lte800"),
    Scenario("analogue-vtx", "Analogue FPV video", "Analogue video carrier on 5.8 GHz", 60, ("vtx",), "5g8"),
    Scenario("pmr446", "PMR446 handheld", "Voice transmissions on 446 MHz", 45, ("pmr446",), "pmr446"),
)}


class DemoUnavailable(RuntimeError):
    """Raised when a scenario can't run (a real SDR is attached)."""
