"""Signal-of-interest rules.

A rule says: "energy this far over the noise floor, inside this band, with
this shape, for this long, is worth an event". Rules only look at energy and
bandwidth: nothing here demodulates or decodes a transmission.

kind:
  fixed    - a signal that stays on one frequency (LoRa node, VTX, PMR446)
  hopping  - many short narrow hits spread across a band (ELRS, Bluetooth)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

MHZ = 1e6
KHZ = 1e3


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    lo_mhz: float
    hi_mhz: float
    kind: str = "fixed"  # fixed | hopping
    min_snr_db: float = 10.0
    min_bw_khz: float = 0.0
    max_bw_khz: float = 1e9
    min_duration_s: float = 0.3
    min_hops: int = 5  # hopping only: distinct channels seen within hop_window_s
    hop_window_s: float = 1.0
    likely: str = ""  # what a trainee should think this is
    advice: str = ""  # EMCON advice shown in the debrief

    @property
    def lo_hz(self) -> float:
        return self.lo_mhz * MHZ

    @property
    def hi_hz(self) -> float:
        return self.hi_mhz * MHZ

    def matches_shape(self, freq_hz: float, bw_hz: float, snr_db: float) -> bool:
        return (
            self.lo_hz <= freq_hz <= self.hi_hz
            and self.min_bw_khz * KHZ <= bw_hz <= self.max_bw_khz * KHZ
            and snr_db >= self.min_snr_db
        )

    def overlaps(self, lo_hz: float, hi_hz: float) -> bool:
        return self.lo_hz < hi_hz and self.hi_hz > lo_hz

    def to_dict(self) -> dict:
        return asdict(self)


DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        id="meshtastic_eu",
        name="LoRa 869.4-869.65 (Meshtastic EU)",
        lo_mhz=869.40, hi_mhz=869.65, kind="fixed",
        min_bw_khz=100, max_bw_khz=350, min_duration_s=0.3,
        likely="Meshtastic node on the EU_868 default slot (LongFast, 250 kHz LoRa)",
        advice="Lengthen position/telemetry broadcast intervals, cut TX power, "
        "and remember every relay node re-broadcasts the same packet.",
    ),
    Rule(
        id="lora_g1",
        name="LoRa/SRD 868.0-868.6 (LoRaWAN, sensors)",
        lo_mhz=868.0, hi_mhz=868.6, kind="fixed",
        min_bw_khz=50, max_bw_khz=300, min_duration_s=0.1,
        likely="LoRaWAN sensor, alarm or telemetry device",
        advice="Short bursts still give away presence and pattern of life.",
    ),
    Rule(
        id="hopper_868",
        name="Frequency hopper 863-870 (ELRS / RC link)",
        lo_mhz=863.0, hi_mhz=870.0, kind="hopping",
        max_bw_khz=800, min_hops=5, hop_window_s=1.0, min_duration_s=0.5,
        likely="ExpressLRS 868 / long-range RC control link (hops many times per second)",
        advice="An RC link is on whenever the transmitter is on. Use dynamic power, "
        "keep the handset off until needed, and lower packet rate/telemetry.",
    ),
    Rule(
        id="hopper_915",
        name="Frequency hopper 902-928 (ELRS 915 / US gear)",
        lo_mhz=902.0, hi_mhz=928.0, kind="hopping",
        max_bw_khz=700, min_hops=5, hop_window_s=1.0, min_duration_s=0.5,
        likely="ELRS/Crossfire set to a US (915) region",
        advice="US-region RC gear transmits outside UK SRD allocations: set the EU 868 domain.",
    ),
    Rule(
        id="srd_433",
        name="433 MHz SRD burst",
        lo_mhz=433.05, hi_mhz=434.79, kind="fixed",
        max_bw_khz=600, min_duration_s=0.1,
        likely="Key fob, weather station, alarm sensor or 433 MHz LoRa/RC",
        advice="Treat unknown 433 MHz emitters as civilian until proven otherwise.",
    ),
    Rule(
        id="pmr446",
        name="PMR446 voice",
        lo_mhz=446.0, hi_mhz=446.2, kind="fixed",
        max_bw_khz=30, min_duration_s=0.5,
        likely="PMR446 handheld (push-to-talk)",
        advice="Every PTT press is a beacon. Use brevity, low power and scheduled comms windows.",
    ),
    Rule(
        id="lte800_ul",
        name="Mobile phone uplink (LTE 800)",
        lo_mhz=832.0, hi_mhz=862.0, kind="fixed",
        min_bw_khz=150, min_duration_s=0.2,
        likely="Mobile phone talking to a mast",
        advice="Phones transmit even when you are not using them. Flight mode, or leave them behind.",
    ),
    Rule(
        id="hopper_2g4",
        name="Frequency hopper 2.4 GHz (ELRS 2.4 / Bluetooth)",
        lo_mhz=2400.0, hi_mhz=2483.5, kind="hopping",
        max_bw_khz=1500, min_hops=8, hop_window_s=1.0, min_duration_s=0.5,
        likely="ExpressLRS 2.4 control link, Bluetooth or other hopper",
        advice="Same as any RC link: on whenever the handset is on.",
    ),
    Rule(
        id="wifi_2g4",
        name="Wi-Fi 2.4 GHz",
        lo_mhz=2400.0, hi_mhz=2483.5, kind="fixed",
        min_bw_khz=12000, min_duration_s=0.2,
        likely="Wi-Fi access point, phone hotspot or digital video link",
        advice="Disable hotspots and Wi-Fi on phones, GoPros and 360 cameras.",
    ),
    Rule(
        id="dji_video_5g8",
        name="Digital FPV video 5.8 GHz (DJI O3/O4-style)",
        lo_mhz=5725.0, hi_mhz=5850.0, kind="fixed",
        min_bw_khz=8000, max_bw_khz=45000, min_duration_s=2.0,
        likely="Digital FPV video downlink (DJI O3/O4, Walksnail, HDZero): persistent 10/20/40 MHz block",
        advice="Digital video links transmit continuously while the air unit is powered, even on the "
        "ground. Power the air unit only when needed and use the lowest power setting.",
    ),
    Rule(
        id="vtx_5g8",
        name="Analog FPV video 5.8 GHz",
        lo_mhz=5640.0, hi_mhz=5950.0, kind="fixed",
        min_bw_khz=3000, max_bw_khz=7000, min_duration_s=1.0,
        likely="Analog FPV video transmitter (continuous carrier)",
        advice="A VTX is a 100% duty-cycle beacon. Use pit/low-power mode on the ground "
        "and only power the VTX when airborne.",
    ),
)


def load_rules(path: str | None) -> tuple[Rule, ...]:
    """Load rules from a JSON list (same fields as Rule) or return defaults."""
    if not path:
        return DEFAULT_RULES
    data = json.loads(Path(path).read_text())
    return tuple(Rule(**item) for item in data)
