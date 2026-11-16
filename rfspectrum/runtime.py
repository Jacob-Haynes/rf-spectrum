"""Glue: source thread -> engine -> alerts / event log / WebSocket fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import numpy as np

from rfspectrum import __version__
from rfspectrum.alerts.cot import DRONE_UID, build_drone_cot, build_emitter_cot, emitter_uid
from rfspectrum.alerts.heartbeat import Heartbeat, short_hostname
from rfspectrum.alerts.mqttbus import MqttBus
from rfspectrum.alerts.tak_client import TakClient
from rfspectrum.alerts.mesh import MeshPublisher, drone_alert_text, emitter_alert_text
from rfspectrum.bands import PRESETS, get_preset
from rfspectrum.config import Settings
from rfspectrum.drone import DroneLinkDetector
from rfspectrum.engine import Emitter, Engine
from rfspectrum.frame import Frame
from rfspectrum.rules import load_rules
from rfspectrum.simulator import SimulatedEnvironment
from rfspectrum.sources import Source, detect_hardware, pick_source
from rfspectrum.sources.rtlsdr import RtlSdrSource
from rfspectrum.sources.simulated import SimulatedSource
from rfspectrum.sources.subprocess_sweep import HackRfSweepSource, RtlPowerSource

logger = logging.getLogger(__name__)


def build_sources(settings: Settings, sim: SimulatedEnvironment | None) -> dict[str, Source]:
    if settings.backend == "sim":
        assert sim is not None
        return {"sim": SimulatedSource(sim)}
    rtl = lambda: RtlSdrSource(settings.rtl_gain, settings.rtl_ppm)  # noqa: E731
    rtlp = lambda: RtlPowerSource(settings.rtl_gain, settings.rtl_ppm)  # noqa: E731
    hack = lambda: HackRfSweepSource(settings.hackrf_lna, settings.hackrf_vga, settings.hackrf_amp)  # noqa: E731
    if settings.backend == "rtlsdr":
        return {"rtlsdr": rtl()}
    if settings.backend == "rtl_power":
        return {"rtl_power": rtlp()}
    if settings.backend == "hackrf":
        return {"hackrf": hack()}
    # auto
    hw = detect_hardware()
    sources: dict[str, Source] = {}
    if hw["rtlsdr"]:
        if RtlSdrSource.available():
            sources["rtlsdr"] = rtl()
        elif RtlPowerSource().available():
            sources["rtl_power"] = rtlp()
        else:
            logger.warning("RTL-SDR attached but neither pyrtlsdr/librtlsdr nor rtl_power is usable")
    if hw["hackrf"]:
        if HackRfSweepSource().available():
            sources["hackrf"] = hack()
        else:
            logger.warning("HackRF attached but hackrf_sweep not installed (INSTALL_HACKRF=1)")
    return sources


class Runtime:
    def __init__(self, settings: Settings, start_thread: bool = True):
        self.settings = settings
        self.rules = load_rules(settings.rules_file or None)
        self.sim: SimulatedEnvironment | None = None
        if settings.backend in ("sim", "auto"):
            self.sim = SimulatedEnvironment(
                seed=settings.sim_seed, background=settings.sim_background, scenario=settings.sim_scenario
            )
        self.sources = build_sources(settings, self.sim)
        if not self.sources:
            logger.warning("No SDR found: falling back to SIMULATED mode")
            settings.backend = "sim"
            self.sources = build_sources(settings, self.sim)
        if settings.backend != "sim":
            self.sim = None
        self.engine = Engine(
            rules=self.rules,
            threshold_db=settings.threshold_db,
            hold_s=settings.hold_s,
            window_s=settings.window_s,
            on_event=self._on_event,
        )
        if settings.backend == "sim":
            self.engine.event_extra = {"simulated": True}
        self.drone = DroneLinkDetector()
        self._drone_t = 0.0
        self.preset_id = settings.preset if settings.preset in PRESETS else "868"
        self.scan = False
        self._scan_since = time.time()
        self._retune = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_thread = start_thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue] = set()
        self._frame_count = 0
        self.fps = 0.0
        self._fps_t = time.time()
        self._fps_n = 0
        self.status_note = ""
        self.active_source: Source | None = None

        self.data_dir = Path(settings.data_dir)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self._event_log: Path | None = self.data_dir / "events.jsonl"
        except OSError as e:
            logger.warning("Cannot use data dir %s (%s); event log not persisted", self.data_dir, e)
            self._event_log = None

        # One MQTT connection for heartbeat + mesh; only connects if one of them is on.
        self.bus = MqttBus(settings.mqtt_host, settings.mqtt_port, settings.mqtt_user, settings.mqtt_pass)
        self.mesh = (
            MeshPublisher(
                self.bus, to=settings.mesh_to, channel=settings.mesh_channel,
                min_interval_s=settings.alert_min_interval_s,
            )
            if settings.alert_mesh
            else None
        )
        self.has_position = settings.sensor_lat is not None and settings.sensor_lon is not None
        # Contract v5: one registered, long-lived connection to OpenTAKServer
        self.cot = (
            TakClient(settings.tak_host, settings.tak_port, short_hostname(),
                      settings.sensor_lat, settings.sensor_lon, version=__version__)
            if settings.alert_cot else None
        )
        if self.cot and not self.has_position:
            logger.warning("ALERT_COT=1 but SENSOR_LAT/SENSOR_LON are not set: no CoT markers will be "
                           "sent (energy detection has no position of its own, and 0,0 is never used)")
        self.heartbeat = Heartbeat(
            self.health, settings.port, self.bus, interval_s=settings.heartbeat_s,
            public_url=settings.public_url,
        )

    # ------------------------------------------------------------ lifecycle

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start(self) -> None:
        if self._start_thread and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sdr", daemon=True)
            self._thread.start()
            self.heartbeat.start()

    def health(self) -> tuple[str, str]:
        """(mode, status) for the launcher heartbeat: mode simulate|cpu, status ok|degraded|error."""
        mode = "simulate" if self.settings.backend == "sim" else "cpu"
        if self._thread is not None and not self._thread.is_alive() and not self._stop.is_set():
            return mode, "error"
        if self.status_note:
            return mode, "degraded"
        return mode, "ok"

    def stop(self) -> None:
        self._stop.set()
        self._retune.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.heartbeat.stop()
        for src in self.sources.values():
            src.close()
        if self.cot:
            self.cot.close()
        self.bus.stop()

    # ------------------------------------------------------------ control

    @property
    def mode(self) -> str:
        return "simulated" if self.settings.backend == "sim" else "live"

    def preset_available(self, preset_id: str) -> bool:
        return pick_source(get_preset(preset_id), self.sources) is not None

    def set_preset(self, preset_id: str) -> None:
        get_preset(preset_id)
        if preset_id != self.preset_id:
            self.preset_id = preset_id
            self._scan_since = time.time()
            self._retune.set()

    def set_scan(self, on: bool) -> None:
        self.scan = on
        self._scan_since = time.time()

    def _maybe_scan(self) -> None:
        if not self.scan or time.time() - self._scan_since < self.settings.scan_dwell_s:
            return
        ids = [p for p in PRESETS if self.preset_available(p)]
        if ids:
            i = ids.index(self.preset_id) if self.preset_id in ids else -1
            self.set_preset(ids[(i + 1) % len(ids)])

    # ------------------------------------------------------------ main loop

    def _run(self) -> None:
        while not self._stop.is_set():
            self._retune.clear()
            preset = get_preset(self.preset_id)
            src = pick_source(preset, self.sources)
            self.active_source = src
            if src is None:
                self.status_note = f"No receiver can tune {preset.name} (needs HackRF)"
                self._retune.wait(1.0)
                self._maybe_scan()
                continue
            self.status_note = ""
            try:
                for frame in src.frames(preset, lambda: self._retune.is_set() or self._stop.is_set()):
                    self.handle_frame(frame)
                    if self.sim is not None:
                        want = self.sim.scenario_tick(frame.ts)
                        if want and want != self.preset_id:
                            self.set_preset(want)
                    self._maybe_scan()
            except Exception as e:
                logger.exception("source %s failed", src.name)
                self.status_note = f"{src.label} error: {e}"
                src.close()
                self._stop.wait(2.0)

    def handle_frame(self, frame: Frame) -> list[dict]:
        events = self.engine.process(frame)
        if frame.ts - self._drone_t >= 1.0:
            self._drone_t = frame.ts
            dev = self.drone.evaluate(self.engine.get_emitters(frame.ts), frame.ts)
            if dev is not None:
                events.append(self._on_drone(dev))
        self._frame_count += 1
        self._fps_n += 1
        now = time.time()
        if now - self._fps_t >= 2.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_t, self._fps_n = now, 0
        if self._loop is not None and self._subs:
            msg = self._frame_message(frame)
            self._loop.call_soon_threadsafe(self._fanout, msg)
            for ev in events:
                self._loop.call_soon_threadsafe(self._fanout, json.dumps({"t": "event", "event": ev}))
        return events

    def _frame_message(self, frame: Frame) -> str:
        msg = {
            "t": "frame",
            "ts": frame.ts,
            "preset": frame.preset_id,
            "p": np.round(frame.power_db, 1).tolist(),
            "hits": [
                {"f": round(h.freq_hz / 1e6, 4), "bw": round(h.bw_hz / 1e3, 1), "snr": round(h.snr_db, 1)}
                for h in self.engine.last_hits[:50]
            ],
        }
        if self._frame_count % 5 == 1:
            floor = self.engine.last_floor.get(frame.preset_id)
            if floor is not None:
                msg["nf"] = np.round(floor, 1).tolist()
                msg["thr"] = self.settings.threshold_db
        return json.dumps(msg)

    # ------------------------------------------------------------ fan-out

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _fanout(self, msg: str) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()  # slow client: drop oldest frame
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    # ------------------------------------------------------------ events & alerts

    def _watched(self, em: Emitter) -> bool:
        f = em.freq_mhz * 1e6
        return any(lo <= f <= hi for lo, hi in self.settings.watch_bands)

    @property
    def simulated(self) -> bool:
        return self.settings.backend == "sim"

    def _log_event(self, ev: dict) -> None:
        if self._event_log is not None:
            try:
                with self._event_log.open("a") as fh:
                    fh.write(json.dumps(ev) + "\n")
            except OSError as e:
                logger.warning("event log write failed: %s", e)

    def _on_drone(self, ev: dict) -> dict:
        ev = self.engine.record_event(ev)
        self._log_event(ev)
        logger.warning("%s%s (%s)", "[SIM] " if self.simulated else "", ev["name"], ev["confidence"])
        if ev["type"] == "drone" and ev.get("escalation") and self.settings.alert_drone:
            if self.mesh:
                self.mesh.send(drone_alert_text(ev, self.simulated))
            if self.cot and self.has_position:
                self.cot.send(build_drone_cot(ev, self.settings.sensor_lat, self.settings.sensor_lon,
                                              self.simulated), uid=DRONE_UID)
        return ev

    def _on_event(self, ev: dict, em: Emitter) -> None:
        self._log_event(ev)
        if ev["type"] != "start" or not ev["new_emitter"] or em.key in self.engine.baseline:
            return
        if not self._watched(em):
            return
        info = {
            "key": em.key, "name": em.name, "freq_mhz": em.freq_mhz, "bw_khz": em.bw_khz,
            "peak_snr_db": em.peak_snr_db, "likely": em.likely,
        }
        if self.mesh:
            self.mesh.send(emitter_alert_text(info, self.simulated))
        if self.cot and self.has_position:
            self.cot.send(build_emitter_cot(
                info, self.settings.sensor_lat, self.settings.sensor_lon, simulated=self.simulated),
                uid=emitter_uid(info))

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        src = self.active_source or next(iter(self.sources.values()), None)
        return {
            "t": "status",
            "mode": self.mode,
            "source": src.label if src else "none",
            "sources": [s.label for s in self.sources.values()],
            "preset": self.preset_id,
            "presets": [
                dict(p.to_dict(), available=self.preset_available(p.id)) for p in PRESETS.values()
            ],
            "scan": self.scan,
            "scan_dwell_s": self.settings.scan_dwell_s,
            "fps": round(self.fps, 1),
            "note": self.status_note,
            "threshold_db": self.settings.threshold_db,
            "emcon": self.engine.get_emcon(),
            "emitters": self.engine.get_emitters(),
            "sim": self.sim.state() if self.sim else None,
            "drone": dict(self.drone.state(), simulated=self.simulated),
            "gemini": {"enabled": self.settings.gemini_enabled, "model": self.settings.gemini_model},
            "alerts": {
                "mesh": self.settings.alert_mesh,
                "cot": self.settings.alert_cot,
                "watch_bands_mhz": [[lo / 1e6, hi / 1e6] for lo, hi in self.settings.watch_bands],
                "tak": f"{self.settings.tak_host}:{self.settings.tak_port}",
                "cot_connected": bool(self.cot and self.cot.registered),
                "sensor_position": self.has_position,
            },
            "rules": [r.to_dict() for r in self.rules],
        }
