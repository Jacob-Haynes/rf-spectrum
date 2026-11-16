"""Synthetic RF environment for --simulate.

Builds power spectra directly in the frequency domain (no IQ needed), with
10 ms sub-slots inside every 100 ms frame so bursty and hopping emitters
look the way they do on real hardware:

- Meshtastic node: 250 kHz LoRa chirp-spread bursts at 869.525 MHz
- ExpressLRS-like control link: 500 kHz packets hopping 13 channels (868)
  or 80 channels (2.4 GHz), 100-250 packets per second
- Analog FPV VTX: continuous FM video carrier at 5.8 GHz
- FPV drone (power-on / flight): ELRS 2.4 hopping plus a DJI O4-style
  10/20 MHz OFDM video block at 5795 MHz
- Phone: LTE 800 uplink bursts plus Wi-Fi probe bursts at 2.4 GHz
- PMR446 handheld: push-to-talk FM voice
- Background (not ours): 433.92 MHz weather station, 868.3 MHz alarm
  sensor, neighbour's Wi-Fi AP

Each frame is the mean (RTL-SDR presets) or max (HackRF presets, where
hackrf_sweep samples each segment briefly) of its sub-slots.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np

from rfspectrum.bands import PRESETS, Preset
from rfspectrum.grid import db

MHZ = 1e6
KHZ = 1e3
FLOOR_DB = -100.0
SUBSLOTS = 10


def shape_flat(freqs: np.ndarray, fc: float, bw: float, bin_hz: float, skirt: float | None = None) -> np.ndarray:
    """Flat-topped occupied bandwidth with Gaussian skirts and -30 dB shoulders.

    Normalised so the value per bin is the in-band SNR scaled by how much of
    the bin the signal fills (a 12.5 kHz signal in a 300 kHz bin is weaker
    per bin than in a 1 kHz bin, as on a real FFT).
    """
    eff = max(bw, bin_hz)
    fill = min(1.0, bw / bin_hz)
    skirt = skirt if skirt is not None else max(eff * 0.06, bin_hz * 0.6)
    d = np.maximum(0.0, np.abs(freqs - fc) - eff / 2)
    return fill * (np.exp(-0.5 * (d / skirt) ** 2) + 1e-3 * np.exp(-d / eff))


def shape_gauss(freqs: np.ndarray, fc: float, sigma: float, bin_hz: float) -> np.ndarray:
    sigma = max(sigma, bin_hz * 0.6)
    return np.exp(-0.5 * ((freqs - fc) / sigma) ** 2)


@dataclass
class Burst:
    start: float
    end: float
    fc: float
    snr_db: float
    bw: float


class SimEmitter:
    id = "base"
    label = "base"
    group = "own"  # own | background
    lo_hz = 0.0
    hi_hz = 0.0

    def __init__(self, rng: np.random.Generator, enabled: bool = False):
        self.rng = rng
        self.enabled = enabled
        self._bursts: list[Burst] = []
        self._next_start: float | None = None

    def visible_in(self, preset: Preset) -> bool:
        return self.lo_hz < preset.hi_hz and self.hi_hz > preset.lo_hz

    def set_enabled(self, on: bool, t: float) -> None:
        if on and not self.enabled:
            self._bursts.clear()
            self._next_start = t + self.first_delay()
        self.enabled = on

    def first_delay(self) -> float:
        return float(self.rng.uniform(0.5, 2.0))

    # Burst-scheduled emitters override next_burst(); continuous/hopping ones override add().
    def next_burst(self, start: float) -> tuple[Burst, float]:
        raise NotImplementedError

    def _schedule(self, t1: float) -> None:
        if self._next_start is None:
            self._next_start = 0.0
        while self._next_start < t1:
            burst, gap = self.next_burst(self._next_start)
            self._bursts.append(burst)
            self._next_start = burst.end + gap

    def add(self, freqs: np.ndarray, preset: Preset, t0: float, t1: float, out: np.ndarray) -> None:
        self._schedule(t1)
        self._bursts = [b for b in self._bursts if b.end > t0 - 5]
        for b in self._bursts:
            overlap = min(b.end, t1) - max(b.start, t0)
            if overlap > 0:
                frac = overlap / (t1 - t0)
                out += frac * 10 ** (b.snr_db / 10) * shape_flat(freqs, b.fc, b.bw, preset.bin_hz)


class Meshtastic(SimEmitter):
    id, label, group = "meshtastic", "Meshtastic node (868)", "own"
    lo_hz, hi_hz = 869.4 * MHZ, 869.65 * MHZ

    def next_burst(self, start):
        airtime = float(self.rng.uniform(0.9, 1.6))  # LongFast SF11/250 kHz
        burst = Burst(start, start + airtime, 869.525 * MHZ, float(self.rng.uniform(26, 34)), 250 * KHZ)
        if self.rng.random() < 0.35:  # a neighbour node relays it ~1-2 s later
            relay_start = burst.end + float(self.rng.uniform(0.8, 2.0))
            self._bursts.append(Burst(relay_start, relay_start + airtime, burst.fc, burst.snr_db - 8, burst.bw))
            return burst, float(self.rng.uniform(8, 18)) + (relay_start - burst.end) + airtime
        return burst, float(self.rng.uniform(6, 16))


class Hopper(SimEmitter):
    """ELRS-like: fixed packet rate, pseudo-random channel per packet."""

    channels: np.ndarray = np.array([])
    rate_hz = 100.0
    airtime_s = 0.004
    bw = 500 * KHZ
    snr_db = 35.0
    telem_ratio = 8

    def __init__(self, rng, enabled=False):
        super().__init__(rng, enabled)
        self.seq = rng.integers(0, len(self.channels), size=4096)

    def add(self, freqs, preset, t0, t1, out):
        dt = t1 - t0
        k0 = math.ceil(t0 * self.rate_hz)
        k1 = math.ceil(t1 * self.rate_hz)
        for k in range(k0, k1):
            ch = self.channels[self.seq[k % len(self.seq)]]
            snr = self.snr_db + float(self.rng.normal(0, 1.5))
            if k % self.telem_ratio == 0:
                snr -= 12  # telemetry from the far end (the drone), weaker
            out += (self.airtime_s / dt) * 10 ** (snr / 10) * shape_flat(freqs, ch, self.bw, preset.bin_hz)


class Elrs868(Hopper):
    id, label, group = "elrs868", "ELRS control link (868)", "own"
    channels = 863.275 * MHZ + np.arange(13) * 0.525 * MHZ
    lo_hz, hi_hz = 863.0 * MHZ, 870.0 * MHZ
    rate_hz, airtime_s, bw, snr_db = 50.0, 0.009, 500 * KHZ, 34.0  # 50 Hz long-range mode


class Elrs2g4(Hopper):
    id, label, group = "elrs2g4", "ELRS control link (2.4 GHz)", "own"
    channels = 2400.4 * MHZ + np.arange(80) * 1.0 * MHZ
    lo_hz, hi_hz = 2400.0 * MHZ, 2483.5 * MHZ
    rate_hz, airtime_s, bw, snr_db = 250.0, 0.002, 812.5 * KHZ, 34.0


class Vtx(SimEmitter):
    id, label, group = "vtx", "FPV analog VTX (5.8 GHz)", "own"
    fc = 5800 * MHZ  # Fatshark/F4
    lo_hz, hi_hz = fc - 10 * MHZ, fc + 10 * MHZ

    def add(self, freqs, preset, t0, t1, out):
        fc = self.fc + float(self.rng.normal(0, 0.25 * MHZ))  # FM deviation wobble
        sig = 10 ** (40 / 10) * shape_gauss(freqs, fc, 2.6 * MHZ, preset.bin_hz)
        sig += 10 ** (44 / 10) * shape_gauss(freqs, fc, 0.3 * MHZ, preset.bin_hz)  # carrier
        sig += 10 ** (22 / 10) * shape_gauss(freqs, fc + 6.5 * MHZ, 0.15 * MHZ, preset.bin_hz)  # audio
        out += sig


class Phone(SimEmitter):
    """LTE 800 uplink (PUCCH edges + PUSCH blocks) and Wi-Fi probe bursts."""

    id, label, group = "phone", "Smartphone (LTE 800 + Wi-Fi)", "own"
    lo_hz, hi_hz = 832 * MHZ, 2483.5 * MHZ
    carrier_lo, carrier_hi = 842 * MHZ, 852 * MHZ

    def visible_in(self, preset):
        return preset.id in ("lte800", "2g4")

    def next_burst(self, start):
        if self.rng.random() < 0.15:
            dur = float(self.rng.uniform(1.0, 3.0))  # app sync / data session
        else:
            dur = float(self.rng.uniform(0.05, 0.3))  # signalling
        return Burst(start, start + dur, 0.0, 26.0, 0.0), float(self.rng.uniform(1.5, 6.0))

    def add(self, freqs, preset, t0, t1, out):
        self._schedule(t1)
        self._bursts = [b for b in self._bursts if b.end > t0 - 5]
        dt = t1 - t0
        for b in self._bursts:
            overlap = min(b.end, t1) - max(b.start, t0)
            if overlap <= 0:
                continue
            frac = overlap / dt
            if preset.id == "lte800":
                width = float(self.rng.choice([0.36, 0.72, 1.44, 2.88, 4.5])) * MHZ
                off = float(self.rng.uniform(self.carrier_lo + 0.5 * MHZ + width / 2, self.carrier_hi - 0.5 * MHZ - width / 2))
                g = 10 ** (b.snr_db / 10) * frac
                out += g * shape_flat(freqs, off, width, preset.bin_hz)
                for edge in (self.carrier_lo + 0.09 * MHZ, self.carrier_hi - 0.09 * MHZ):
                    out += 0.5 * g * shape_flat(freqs, edge, 180 * KHZ, preset.bin_hz)
            elif preset.id == "2g4" and self.rng.random() < 0.6:
                ch = float(self.rng.choice([2412, 2437, 2462])) * MHZ
                out += 0.3 * 10 ** (38 / 10) * shape_flat(freqs, ch, 16.6 * MHZ, preset.bin_hz, skirt=1.5 * MHZ)


class FpvDronePower(SimEmitter):
    """5" FPV on the bench: ELRS 2.4 link up and a DJI O4-style air unit idling.

    ELRS: 812 kHz LoRa packets hopping 80 channels at 250 Hz.
    Video: persistent OFDM-like block, 10 MHz wide at low power on the ground.
    """

    id, label, group = "drone_power", "FPV drone power-on (ELRS 2.4 + O4 idle)", "own"
    lo_hz, hi_hz = 2400 * MHZ, 5850 * MHZ
    video_fc, video_bw, video_snr, fade_db = 5795 * MHZ, 10 * MHZ, 24.0, 1.0

    def __init__(self, rng, enabled=False):
        super().__init__(rng, enabled)
        self.elrs = Elrs2g4(rng, True)
        self._fade = 0.0

    def visible_in(self, preset):
        return preset.id == "2g4" or (preset.lo_hz < 5850 * MHZ and preset.hi_hz > 5725 * MHZ)

    def add(self, freqs, preset, t0, t1, out):
        if preset.lo_hz < 2483.5 * MHZ and preset.hi_hz > 2400 * MHZ:
            self.elrs.add(freqs, preset, t0, t1, out)
        if preset.lo_hz < self.video_fc + self.video_bw and preset.hi_hz > self.video_fc - self.video_bw:
            self._fade = 0.98 * self._fade + float(self.rng.normal(0, self.fade_db * 0.2))
            snr = self.video_snr + self._fade
            out += 10 ** (snr / 10) * shape_flat(
                freqs, self.video_fc, self.video_bw, preset.bin_hz, skirt=0.35 * MHZ
            )


class FpvDroneFlight(FpvDronePower):
    """Airborne: video at full bandwidth/power with fading as the quad manoeuvres."""

    id, label = "drone_flight", "FPV drone flight (ELRS 2.4 + O4 20 MHz)"
    video_bw, video_snr, fade_db = 20 * MHZ, 36.0, 4.0


class Pmr446(SimEmitter):
    id, label, group = "pmr446", "PMR446 handheld", "own"
    lo_hz, hi_hz = 446.0 * MHZ, 446.2 * MHZ
    channel = 446.00625 * MHZ + 3 * 12.5 * KHZ  # channel 4

    def next_burst(self, start):
        dur = float(self.rng.uniform(2.0, 6.0))
        return Burst(start, start + dur, self.channel, 40.0, 11 * KHZ), float(self.rng.uniform(4, 12))


class WeatherStation(SimEmitter):
    id, label, group = "bg_433", "Weather station 433.92 (civilian)", "background"
    lo_hz, hi_hz = 433.8 * MHZ, 434.0 * MHZ

    def first_delay(self):
        return float(self.rng.uniform(5, 20))

    def next_burst(self, start):
        return Burst(start, start + 0.35, 433.92 * MHZ, 22.0, 80 * KHZ), float(self.rng.uniform(30, 48))


class AlarmSensor(SimEmitter):
    id, label, group = "bg_868", "Alarm sensor 868.3 (civilian)", "background"
    lo_hz, hi_hz = 868.2 * MHZ, 868.4 * MHZ

    def first_delay(self):
        return float(self.rng.uniform(20, 40))

    def next_burst(self, start):
        return Burst(start, start + 0.2, 868.3 * MHZ, 18.0, 125 * KHZ), float(self.rng.uniform(45, 90))


class NeighbourWifi(SimEmitter):
    id, label, group = "bg_wifi", "Neighbour Wi-Fi AP ch 11 (civilian)", "background"
    lo_hz, hi_hz = 2451 * MHZ, 2473 * MHZ

    def add(self, freqs, preset, t0, t1, out):
        # ~1 ms beacons every 102 ms plus occasional traffic: bursty, not continuous
        if self.rng.random() > 0.08:
            return
        busy = 0.3 if self.rng.random() > 0.15 else 1.0
        out += busy * 10 ** (30 / 10) * shape_flat(freqs, 2462 * MHZ, 16.6 * MHZ, preset.bin_hz, skirt=1.5 * MHZ)


EMITTER_CLASSES = (Meshtastic, Elrs868, Elrs2g4, Vtx, FpvDronePower, FpvDroneFlight, Phone, Pmr446, WeatherStation, AlarmSensor, NeighbourWifi)

# Auto scenario: (seconds into the loop, action). Loop repeats.
AUTO_SCENARIO = (
    (0, {"preset": "868", "own": []}),
    (25, {"own": ["meshtastic"]}),
    (55, {"own": ["meshtastic", "elrs868"]}),
    (90, {"preset": "5g8", "own": ["meshtastic", "elrs868", "vtx"]}),
    (115, {"preset": "lte800", "own": ["meshtastic", "elrs868", "vtx", "phone"]}),
    # Drone threat segment: power-on on the 2.4 GHz band, video on 5.8, then flight.
    (140, {"preset": "2g4", "own": ["drone_power"]}),
    (160, {"preset": "5g8", "own": ["drone_power"]}),
    (180, {"preset": "5g8", "own": ["drone_flight"]}),
    (200, {"preset": "2g4", "own": ["drone_flight"]}),
    (220, {"preset": "868", "own": []}),
)
AUTO_LOOP_S = 250
EXCLUSIVE = {"drone_power": "drone_flight", "drone_flight": "drone_power"}


class SimulatedEnvironment:
    def __init__(self, seed: int | None = None, background: bool = True, scenario: str = "auto"):
        self.rng = np.random.default_rng(seed)
        self.emitters = {cls.id: cls(np.random.default_rng(self.rng.integers(1 << 32))) for cls in EMITTER_CLASSES}
        for em in self.emitters.values():
            if em.group == "background":
                em.set_enabled(background, 0.0)
        self.scenario = scenario
        self._scenario_t0: float | None = None
        self._scenario_step = -1
        self._lock = threading.Lock()
        self._ripple_phase = float(self.rng.uniform(0, 2 * math.pi))

    # ------------------------------------------------------------- control

    def set_emitter(self, emitter_id: str, on: bool, t: float, manual: bool = True) -> None:
        with self._lock:
            if emitter_id not in self.emitters:
                raise KeyError(emitter_id)
            if manual:
                self.scenario = "manual"
            if on and emitter_id in EXCLUSIVE:  # one drone: on the bench OR flying
                self.emitters[EXCLUSIVE[emitter_id]].set_enabled(False, t)
            self.emitters[emitter_id].set_enabled(on, t)

    def set_scenario(self, scenario: str, t: float) -> None:
        with self._lock:
            self.scenario = scenario
            self._scenario_t0 = t
            self._scenario_step = -1

    def scenario_tick(self, t: float) -> str | None:
        """Advance the auto scenario. Returns a preset id if it wants a retune."""
        if self.scenario != "auto":
            return None
        with self._lock:
            if self._scenario_t0 is None:
                self._scenario_t0 = t
            pos = (t - self._scenario_t0) % AUTO_LOOP_S
            step = max(i for i, (at, _) in enumerate(AUTO_SCENARIO) if at <= pos)
            if step == self._scenario_step:
                return None
            self._scenario_step = step
            action = AUTO_SCENARIO[step][1]
            for em in self.emitters.values():
                if em.group == "own":
                    em.set_enabled(em.id in action["own"], t)
            return action.get("preset")

    def state(self) -> dict:
        return {
            "scenario": self.scenario,
            "emitters": [
                {"id": e.id, "label": e.label, "group": e.group, "enabled": e.enabled}
                for e in self.emitters.values()
            ],
        }

    # ------------------------------------------------------------- render

    def render(self, preset: Preset | str, t0: float, dt: float = 0.1) -> np.ndarray:
        if isinstance(preset, str):
            preset = PRESETS[preset]
        freqs = preset.freqs_hz()
        x = (freqs - preset.lo_hz) / preset.span_hz
        ripple_db = 1.2 * np.sin(2 * math.pi * 1.3 * x + self._ripple_phase) - 2.0 * (
            np.exp(-x / 0.02) + np.exp(-(1 - x) / 0.02)
        )
        floor_lin = 10 ** ((FLOOR_DB + ripple_db) / 10)
        use_max = preset.needs_hackrf()
        acc = np.zeros(preset.bins)
        sub_dt = dt / SUBSLOTS
        with self._lock:
            active = [e for e in self.emitters.values() if e.enabled and e.visible_in(preset)]
            for s in range(SUBSLOTS):
                a, b = t0 + s * sub_dt, t0 + (s + 1) * sub_dt
                sig = np.zeros(preset.bins)
                for em in active:
                    em.add(freqs, preset, a, b, sig)
                noise = self.rng.gamma(8.0, 1 / 8.0, size=preset.bins)
                sub = floor_lin * (noise + sig)
                acc = np.maximum(acc, sub) if use_max else acc + sub / SUBSLOTS
        return db(acc).astype(np.float32)
