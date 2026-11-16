"""Energy detection, signal-of-interest rules, emitter tracking and EMCON score.

Pipeline per frame:
  noise floor (per bin) -> hits (contiguous bins over the floor + threshold)
  -> rules (fixed / hopping) -> events (start/end) -> emitter registry
  -> EMCON score.

Only power per frequency bin is used. No demodulation, no decoding.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from rfspectrum.bands import PRESETS, Preset
from rfspectrum.frame import Frame
from rfspectrum.rules import DEFAULT_RULES, Rule

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- noise floor


class NoiseFloor:
    """Per-bin noise floor: follows the noise down quickly and rises slowly.

    The per-bin estimate is clamped to (frame median + ripple) so that a
    continuous emitter (e.g. a VTX left on for minutes) is never absorbed into
    the floor and does not "disappear".
    """

    def __init__(self, down: float = 0.3, up: float = 0.01, ripple_db: float = 4.0):
        self.down = down
        self.up = up
        self.ripple_db = ripple_db
        self.floor: np.ndarray | None = None

    def update(self, power_db: np.ndarray) -> np.ndarray:
        p = power_db.astype(np.float64)
        med = float(np.median(p))
        if self.floor is None or self.floor.shape != p.shape:
            self.floor = np.full(p.shape, med)
        diff = p - self.floor
        rate = np.where(diff < 0, self.down, self.up)
        self.floor = self.floor + rate * diff
        self.floor = np.minimum(self.floor, med + self.ripple_db)
        return self.floor


# --------------------------------------------------------------------------- hits


@dataclass
class Hit:
    freq_hz: float
    bw_hz: float
    peak_db: float
    snr_db: float
    lo_hz: float
    hi_hz: float


def find_hits(
    power_db: np.ndarray,
    floor_db: np.ndarray,
    preset: Preset,
    threshold_db: float,
    gap_bins: int = 1,
) -> list[Hit]:
    snr = power_db - floor_db
    idx = np.flatnonzero(snr >= threshold_db)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > gap_bins + 1)
    groups = np.split(idx, breaks + 1)
    freqs = preset.freqs_hz()
    hits = []
    for g in groups:
        lo, hi = int(g[0]), int(g[-1])
        seg = slice(lo, hi + 1)
        lin = 10 ** (power_db[seg] / 10.0)
        centre = float(np.sum(freqs[seg] * lin) / np.sum(lin))
        k = lo + int(np.argmax(power_db[seg]))
        # Occupied bandwidth: bins within 6 dB of the peak (skirts of strong
        # signals cross the threshold far outside the real channel width).
        occupied = int(np.count_nonzero(power_db[seg] >= power_db[k] - 6.0))
        hits.append(
            Hit(
                freq_hz=centre,
                bw_hz=max(1, occupied) * preset.bin_hz,
                peak_db=float(power_db[k]),
                snr_db=float(snr[k]),
                lo_hz=float(freqs[lo] - preset.bin_hz / 2),
                hi_hz=float(freqs[hi] + preset.bin_hz / 2),
            )
        )
    return hits


# --------------------------------------------------------------------------- tracking


@dataclass
class _Track:
    """Activation state for one rule (or one unclassified frequency cluster)."""

    key: str
    run_start: float | None = None
    last_hit: float | None = None
    in_event: bool = False
    peak_snr: float = 0.0
    freq_hz: float = 0.0
    bw_hz: float = 0.0
    lo_hz: float = 0.0
    hi_hz: float = 0.0
    hops: deque = field(default_factory=deque)  # (ts, channel) for hopping rules


@dataclass
class Emitter:
    key: str
    name: str
    rule_id: str | None
    likely: str
    preset_id: str
    freq_mhz: float
    bw_khz: float
    peak_snr_db: float
    first_seen: float
    last_seen: float
    lo_mhz: float = 0.0
    hi_mhz: float = 0.0
    events: int = 0
    active_ts: deque = field(default_factory=deque)

    def to_dict(self, duty: float, now: float, window_s: float, monitored: bool) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "rule_id": self.rule_id,
            "likely": self.likely,
            "preset": self.preset_id,
            "freq_mhz": round(self.freq_mhz, 4),
            "lo_mhz": round(self.lo_mhz, 4),
            "hi_mhz": round(self.hi_mhz, 4),
            "bw_khz": round(self.bw_khz, 1),
            "peak_snr_db": round(self.peak_snr_db, 1),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "events": self.events,
            "duty": round(duty, 4),
            "active": now - self.last_seen <= window_s,
            "monitored": monitored,
        }


def emcon_score(active: list[dict]) -> dict:
    """Score 100 = nothing heard. Each emitter costs 15, the worst duty cycle up to 50."""
    n = len(active)
    max_duty = max((e["duty"] for e in active), default=0.0)
    score = int(round(max(0.0, min(100.0, 100 - 15 * n - 50 * max_duty))))
    if score >= 90:
        grade = "SILENT"
    elif score >= 70:
        grade = "LOW"
    elif score >= 40:
        grade = "ELEVATED"
    else:
        grade = "LOUD"
    return {
        "score": score,
        "grade": grade,
        "emitters": n,
        "max_duty": round(max_duty, 4),
        "total_airtime": round(sum(e["duty"] for e in active), 4),
    }


EventCallback = Callable[[dict, "Emitter"], None]


class Engine:
    def __init__(
        self,
        rules: tuple[Rule, ...] = DEFAULT_RULES,
        threshold_db: float = 10.0,
        hold_s: float = 1.0,
        window_s: float = 60.0,
        unclassified_min_s: float = 2.0,
        max_events: int = 500,
        on_event: EventCallback | None = None,
    ):
        self.rules = rules
        self.threshold_db = threshold_db
        self.hold_s = hold_s
        self.window_s = window_s
        self.unclassified_min_s = unclassified_min_s
        self.on_event = on_event
        self._floors: dict[str, NoiseFloor] = {}
        self._tracks: dict[str, _Track] = {}
        self._frames: dict[str, deque] = {}  # preset -> frame timestamps in window
        self.emitters: dict[str, Emitter] = {}
        self.events: deque = deque(maxlen=max_events)
        self._event_ids = itertools.count(1)
        self._lock = threading.RLock()
        self.last_floor: dict[str, np.ndarray] = {}
        self.last_hits: list[Hit] = []
        self.current_preset: str | None = None
        self.frames_processed = 0
        self.baseline: set[str] = set()  # emitter keys present before own kit was switched on

    # ------------------------------------------------------------------ public

    def process(self, frame: Frame) -> list[dict]:
        preset = PRESETS[frame.preset_id]
        with self._lock:
            self.current_preset = preset.id
            self.frames_processed += 1
            floor = self._floors.setdefault(preset.id, NoiseFloor()).update(frame.power_db)
            self.last_floor[preset.id] = floor
            hits = find_hits(frame.power_db, floor, preset, self.threshold_db)
            self.last_hits = hits
            ts = frame.ts
            frames = self._frames.setdefault(preset.id, deque())
            frames.append(ts)
            self._trim(frames, ts)

            new_events: list[dict] = []
            rules = [r for r in self.rules if r.overlaps(preset.lo_hz, preset.hi_hz)]
            claimed: set[int] = set()

            for rule in (r for r in rules if r.kind == "fixed"):
                matched = [
                    (i, h) for i, h in enumerate(hits) if rule.matches_shape(h.freq_hz, h.bw_hz, h.snr_db)
                ]
                claimed.update(i for i, _ in matched)
                best = max((h for _, h in matched), key=lambda h: h.snr_db, default=None)
                new_events += self._step(rule.id, rule, preset, ts, best, best is not None)

            for rule in (r for r in rules if r.kind == "hopping"):
                tr = self._tracks.setdefault(rule.id, _Track(rule.id))
                quantum = max(rule.max_bw_khz * 500.0, preset.bin_hz)
                hop_hits = [
                    (i, h)
                    for i, h in enumerate(hits)
                    if i not in claimed and rule.matches_shape(h.freq_hz, h.bw_hz, h.snr_db)
                ]
                for _, h in hop_hits:
                    tr.hops.append((ts, int(h.freq_hz // quantum)))
                while tr.hops and tr.hops[0][0] < ts - rule.hop_window_s:
                    tr.hops.popleft()
                distinct = len({c for _, c in tr.hops})
                active = bool(hop_hits) and distinct >= rule.min_hops
                if active or tr.in_event or distinct >= max(2, rule.min_hops // 2):
                    # While a hopper is active, narrow-ish hits in its band are its
                    # own (two hops landing on adjacent channels merge into one hit).
                    claimed.update(
                        i
                        for i, h in enumerate(hits)
                        if rule.lo_hz <= h.freq_hz <= rule.hi_hz and h.bw_hz <= 3 * rule.max_bw_khz * 1e3
                        and i not in claimed
                    )
                best = max((h for _, h in hop_hits), key=lambda h: h.snr_db, default=None)
                if active and best is not None:
                    span_lo = min(h.lo_hz for _, h in hop_hits)
                    span_hi = max(h.hi_hz for _, h in hop_hits)
                    if tr.in_event:
                        span_lo, span_hi = min(span_lo, tr.lo_hz), max(span_hi, tr.hi_hz)
                    best = Hit(
                        freq_hz=(span_lo + span_hi) / 2,
                        bw_hz=span_hi - span_lo,
                        peak_db=best.peak_db,
                        snr_db=best.snr_db,
                        lo_hz=span_lo,
                        hi_hz=span_hi,
                    )
                new_events += self._step(rule.id, rule, preset, ts, best, active)

            # A wideband signal (Wi-Fi, VTX, LTE) breaks into fragments when it is
            # weak or partly overlapped: while its rule is in an event, other
            # hits in that rule's band are treated as part of it, not new emitters.
            for rule in (r for r in rules if r.kind == "fixed" and r.min_bw_khz >= 1000):
                tr = self._tracks.get(rule.id)
                if tr is not None and tr.in_event:
                    claimed.update(
                        i for i, h in enumerate(hits) if rule.lo_hz <= h.freq_hz <= rule.hi_hz
                    )

            # Unclassified: strong, sustained hits that no rule claimed.
            for i, h in enumerate(hits):
                if i in claimed or h.snr_db < self.threshold_db + 3:
                    continue
                cluster = max(h.bw_hz, 4 * preset.bin_hz, 25e3)
                key = f"unk:{preset.id}:{int(round(h.freq_hz / cluster))}"
                new_events += self._step(key, None, preset, ts, h, True)
            # close idle unclassified tracks in this preset
            for key in [k for k in self._tracks if k.startswith(f"unk:{preset.id}:")]:
                tr = self._tracks[key]
                if tr.last_hit is not None and tr.last_hit < ts:
                    new_events += self._step(key, None, preset, ts, None, False)
                if not tr.in_event and tr.last_hit is not None and ts - tr.last_hit > 10:
                    del self._tracks[key]

        for ev in new_events:
            if self.on_event:
                try:
                    self.on_event(ev, self.emitters[ev["emitter"]])
                except Exception:  # alerts must never break detection
                    logger.exception("event callback failed")
        return new_events

    def reset(self) -> None:
        with self._lock:
            self._floors.clear()
            self._tracks.clear()
            self._frames.clear()
            self.emitters.clear()
            self.events.clear()
            self.baseline = set()

    def get_events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self.events)[-limit:][::-1]

    def get_emitters(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        with self._lock:
            out = []
            for em in self.emitters.values():
                frames = self._frames.get(em.preset_id, deque())
                monitored = bool(frames) and now - frames[-1] < 5
                # For a band we are not currently listening to, report the last
                # known state: the window ending when that band was last observed.
                ref = now if monitored or not frames else frames[-1]
                n_frames = sum(1 for t in frames if ref - self.window_s <= t <= ref)
                n_active = sum(1 for t in em.active_ts if ref - self.window_s <= t <= ref)
                duty = min(1.0, n_active / n_frames) if n_frames else 0.0
                d = em.to_dict(duty, ref, self.window_s, monitored)
                d["baseline"] = em.key in self.baseline
                out.append(d)
            out.sort(key=lambda e: (-e["active"], -e["duty"], -e["last_seen"]))
            return out

    def get_emcon(self, now: float | None = None) -> dict:
        emitters = self.get_emitters(now)
        active = [e for e in emitters if e["active"] and not e["baseline"]]
        result = emcon_score(active)
        result["window_s"] = self.window_s
        result["seen_total"] = len(emitters)
        result["baseline"] = len(self.baseline)
        return result

    def set_baseline(self) -> int:
        """Mark every emitter heard so far as environment background (not scored)."""
        with self._lock:
            self.baseline = set(self.emitters)
            return len(self.baseline)

    def clear_baseline(self) -> None:
        with self._lock:
            self.baseline = set()

    # ------------------------------------------------------------------ internals

    def _trim(self, dq: deque, ts: float) -> None:
        while dq and dq[0] < ts - self.window_s:
            dq.popleft()

    def _step(
        self, key: str, rule: Rule | None, preset: Preset, ts: float, hit: Hit | None, active: bool
    ) -> list[dict]:
        tr = self._tracks.setdefault(key, _Track(key))
        events: list[dict] = []
        min_dur = rule.min_duration_s if rule else self.unclassified_min_s

        if active and hit is not None:
            if tr.last_hit is None or ts - tr.last_hit > self.hold_s or tr.run_start is None:
                tr.run_start = ts
                tr.peak_snr = 0.0
            tr.last_hit = ts
            tr.peak_snr = max(tr.peak_snr, hit.snr_db)
            tr.freq_hz, tr.bw_hz = hit.freq_hz, hit.bw_hz
            tr.lo_hz, tr.hi_hz = hit.lo_hz, hit.hi_hz
            if not tr.in_event and ts - tr.run_start >= min_dur:
                tr.in_event = True
                events.append(self._open(key, rule, preset, ts, tr))
            if tr.in_event:
                em = self.emitters[key]
                em.last_seen = ts
                em.active_ts.append(ts)
                self._trim(em.active_ts, ts)
                em.freq_mhz, em.bw_khz = tr.freq_hz / 1e6, tr.bw_hz / 1e3
                em.lo_mhz, em.hi_mhz = tr.lo_hz / 1e6, tr.hi_hz / 1e6
                em.peak_snr_db = max(em.peak_snr_db, hit.snr_db)
        elif tr.in_event and tr.last_hit is not None and ts - tr.last_hit > self.hold_s:
            tr.in_event = False
            events.append(self._close(key, rule, preset, tr))
            tr.run_start = None
        elif not tr.in_event and tr.last_hit is not None and ts - tr.last_hit > self.hold_s:
            tr.run_start = None
        return events

    def _record(self, ev: dict) -> dict:
        ev["id"] = next(self._event_ids)
        self.events.append(ev)
        return ev

    def _open(self, key: str, rule: Rule | None, preset: Preset, ts: float, tr: _Track) -> dict:
        new = key not in self.emitters
        name = rule.name if rule else f"Unclassified {tr.freq_hz / 1e6:.3f} MHz"
        if new:
            self.emitters[key] = Emitter(
                key=key,
                name=name,
                rule_id=rule.id if rule else None,
                likely=rule.likely if rule else "Unknown emitter: no rule matches its shape",
                preset_id=preset.id,
                freq_mhz=tr.freq_hz / 1e6,
                bw_khz=tr.bw_hz / 1e3,
                lo_mhz=tr.lo_hz / 1e6,
                hi_mhz=tr.hi_hz / 1e6,
                peak_snr_db=tr.peak_snr,
                first_seen=ts,
                last_seen=ts,
            )
        em = self.emitters[key]
        em.events += 1
        return self._record(
            {
                "type": "start",
                "ts": ts,
                "emitter": key,
                "rule_id": em.rule_id,
                "name": name,
                "preset": preset.id,
                "freq_mhz": round(tr.freq_hz / 1e6, 4),
                "bw_khz": round(tr.bw_hz / 1e3, 1),
                "snr_db": round(tr.peak_snr, 1),
                "new_emitter": new,
                "duration_s": None,
            }
        )

    def _close(self, key: str, rule: Rule | None, preset: Preset, tr: _Track) -> dict:
        em = self.emitters[key]
        return self._record(
            {
                "type": "end",
                "ts": tr.last_hit,
                "emitter": key,
                "rule_id": em.rule_id,
                "name": em.name,
                "preset": preset.id,
                "freq_mhz": round(tr.freq_hz / 1e6, 4),
                "bw_khz": round(tr.bw_hz / 1e3, 1),
                "snr_db": round(tr.peak_snr, 1),
                "new_emitter": False,
                "duration_s": round((tr.last_hit or 0) - (tr.run_start or 0), 2),
            }
        )
