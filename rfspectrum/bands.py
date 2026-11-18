"""Band presets with UK/EU frequency allocation labels.

Allocation notes are summarised from Ofcom IR 2030 (UK licence-exempt short
range devices) and CEPT ERC Recommendation 70-03. They are training labels,
not legal advice: always check the current Ofcom documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

MHZ = 1_000_000.0

# Hardware tuning limits (Hz)
RTLSDR_MIN_HZ = 24 * MHZ
RTLSDR_MAX_HZ = 1766 * MHZ
HACKRF_MIN_HZ = 1 * MHZ
HACKRF_MAX_HZ = 6000 * MHZ

DEFAULT_BINS = 1024


@dataclass(frozen=True)
class Allocation:
    lo_mhz: float
    hi_mhz: float
    label: str
    note: str = ""
    kind: str = "srd"  # srd | ism | licensed | warn

    def to_dict(self) -> dict:
        return {
            "lo_mhz": self.lo_mhz,
            "hi_mhz": self.hi_mhz,
            "label": self.label,
            "note": self.note,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    lo_mhz: float
    hi_mhz: float
    description: str
    allocations: tuple[Allocation, ...] = field(default_factory=tuple)
    bins: int = DEFAULT_BINS

    @property
    def lo_hz(self) -> float:
        return self.lo_mhz * MHZ

    @property
    def hi_hz(self) -> float:
        return self.hi_mhz * MHZ

    @property
    def span_hz(self) -> float:
        return self.hi_hz - self.lo_hz

    @property
    def bin_hz(self) -> float:
        return self.span_hz / self.bins

    def freqs_hz(self) -> np.ndarray:
        """Centre frequency of every display bin."""
        return self.lo_hz + (np.arange(self.bins) + 0.5) * self.bin_hz

    def needs_hackrf(self) -> bool:
        return self.hi_hz > RTLSDR_MAX_HZ or self.lo_hz < RTLSDR_MIN_HZ

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "lo_mhz": self.lo_mhz,
            "hi_mhz": self.hi_mhz,
            "bins": self.bins,
            "bin_khz": round(self.bin_hz / 1000, 3),
            "description": self.description,
            "needs_hackrf": self.needs_hackrf(),
            "allocations": [a.to_dict() for a in self.allocations],
        }


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in [
        Preset(
            id="pmr446",
            name="PMR446",
            lo_mhz=445.95,
            hi_mhz=446.25,
            description="Licence-free handheld radios (walkie-talkies). "
            "Push-to-talk voice: every press of the PTT is visible.",
            allocations=(
                Allocation(446.0, 446.2, "PMR446 ch 1-16", "0.5 W ERP, 12.5 kHz channels", "srd"),
            ),
        ),
        Preset(
            id="433",
            name="433 MHz SRD",
            lo_mhz=432.8,
            hi_mhz=435.0,
            description="433 MHz ISM/SRD: key fobs, weather sensors, alarms, LoRa 433 "
            "and some long-range RC links. Shares spectrum with 70 cm amateur.",
            allocations=(
                Allocation(432.8, 433.05, "70 cm amateur", "Licensed amateur", "licensed"),
                Allocation(433.05, 434.79, "SRD 433", "10 mW ERP, ISM", "ism"),
                Allocation(434.79, 435.0, "70 cm amateur", "Licensed amateur", "licensed"),
            ),
        ),
        Preset(
            id="868",
            name="868 MHz SRD / LoRa",
            lo_mhz=863.0,
            hi_mhz=870.0,
            description="SRD860: Meshtastic (EU_868), LoRaWAN, ExpressLRS 868, "
            "alarm sensors. Duty-cycle limited sub-bands.",
            allocations=(
                Allocation(863.0, 865.0, "863-865", "Wireless audio / SRD, 25 mW", "srd"),
                Allocation(865.0, 868.0, "865-868", "RFID / SRD, 25 mW", "srd"),
                Allocation(868.0, 868.6, "g1 868.0-868.6", "25 mW, 1% duty (LoRaWAN)", "srd"),
                Allocation(868.7, 869.2, "g2", "25 mW, 0.1% duty", "srd"),
                Allocation(869.4, 869.65, "g3 869.4-869.65", "500 mW, 10% duty (Meshtastic EU default)", "srd"),
                Allocation(869.7, 870.0, "g4", "25 mW", "srd"),
            ),
        ),
        Preset(
            id="915",
            name="902-928 MHz (US ISM)",
            lo_mhz=902.0,
            hi_mhz=928.0,
            description="US ISM band. ELRS/Crossfire '915' gear and US-region "
            "Meshtastic live here. In the UK most of this band is NOT licence-exempt: "
            "set radios to the EU 868 region.",
            allocations=(
                Allocation(902.0, 915.0, "Not UK SRD", "Mobile (GSM/LTE 900 uplink) in UK", "warn"),
                Allocation(915.0, 921.0, "915-921 RFID/SRD", "UK IR 2030 (limited)", "srd"),
                Allocation(921.0, 928.0, "Not UK SRD", "GSM-R / mobile downlink in UK", "warn"),
            ),
        ),
        Preset(
            id="lte800",
            name="Mobile uplink (LTE 800)",
            lo_mhz=832.0,
            hi_mhz=862.0,
            description="Band 20 uplink: what phones transmit to the mast. "
            "A phone that is not in flight mode lights this up.",
            allocations=(
                Allocation(832.0, 842.0, "B20 UL (op A)", "Licensed mobile", "licensed"),
                Allocation(842.0, 852.0, "B20 UL (op B)", "Licensed mobile", "licensed"),
                Allocation(852.0, 862.0, "B20 UL (op C)", "Licensed mobile", "licensed"),
            ),
        ),
        Preset(
            id="2g4",
            name="2.4 GHz ISM",
            lo_mhz=2400.0,
            hi_mhz=2483.5,
            description="Wi-Fi, Bluetooth, ExpressLRS 2.4, DJI/digital links. "
            "HackRF required (above RTL-SDR range).",
            allocations=(
                Allocation(2401.0, 2423.0, "Wi-Fi ch 1", "", "ism"),
                Allocation(2426.0, 2448.0, "Wi-Fi ch 6", "", "ism"),
                Allocation(2451.0, 2473.0, "Wi-Fi ch 11", "", "ism"),
                Allocation(2400.0, 2483.5, "ISM 2.4 GHz", "100 mW EIRP", "ism"),
            ),
        ),
        Preset(
            id="5g8",
            name="5.8 GHz FPV video",
            lo_mhz=5640.0,
            hi_mhz=5950.0,
            description="Analog FPV video transmitters (VTX) and 5.8 GHz Wi-Fi. "
            "HackRF required.",
            allocations=(
                Allocation(5640.0, 5725.0, "Below SRD band", "Raceband R1/R2 sit here", "warn"),
                Allocation(5725.0, 5875.0, "SRD 5.8 GHz", "25 mW EIRP (IR 2030)", "srd"),
                Allocation(5875.0, 5950.0, "Above SRD band", "Raceband R8 sits here", "warn"),
            ),
        ),
    ]
}

DEFAULT_PRESET = "868"


def get_preset(preset_id: str) -> Preset:
    try:
        return PRESETS[preset_id]
    except KeyError:
        raise KeyError(f"Unknown preset {preset_id!r}; choose from {sorted(PRESETS)}") from None
