"""Plain CoT XML over TCP to a TAK server, with reconnect + backoff.

Energy detection gives no bearing or range, so the marker is placed at the
sensor's own position (SENSOR_LAT/SENSOR_LON) with a large CE and the
frequency details in the remarks.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

logger = logging.getLogger(__name__)

STALE_S = 300


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def build_drone_cot(
    ev: dict, lat: float, lon: float, simulated: bool = False, now: float | None = None
) -> str:
    """Unknown-air marker at the sensor position for a drone link signature."""
    from rfspectrum.drone import describe

    now = time.time() if now is None else now
    tag = "SIMULATED: " if simulated else ""
    remarks = (
        f"{tag}RF: {describe(ev)}, bearing unknown. {ev['name']}. "
        "Energy detection only; marker is at the sensor, not the drone."
    )
    callsign = ("SIM " if simulated else "") + "RF drone link"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="RFSPEC-DRONE" type="a-u-A" how="m-g"'
        f' time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(now + STALE_S)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="2000" le="9999999"/>'
        "<detail>"
        f"<contact callsign={quoteattr(callsign)}/>"
        f"<remarks>{escape(remarks)}</remarks>"
        "</detail></event>"
    )


def build_emitter_cot(
    emitter: dict, lat: float, lon: float, now: float | None = None, uid_prefix: str = "RFSPEC",
    simulated: bool = False,
) -> str:
    now = time.time() if now is None else now
    uid = f"{uid_prefix}-{emitter['key'].replace(':', '-')}"
    callsign = ("SIM " if simulated else "") + f"RF {emitter['freq_mhz']:.3f}"
    remarks = (
        ("SIMULATED: " if simulated else "")
        + f"RF emitter (energy detection only, position = sensor): {emitter['name']}. "
        f"{emitter['freq_mhz']:.3f} MHz, ~{emitter['bw_khz']:.0f} kHz wide, "
        f"peak {emitter['peak_snr_db']:.0f} dB over noise. Likely: {emitter.get('likely', '')}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(uid)} type="a-u-G" how="m-g"'
        f' time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(now + STALE_S)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="1000" le="9999999"/>'
        "<detail>"
        f"<contact callsign={quoteattr(callsign)}/>"
        f"<remarks>{escape(remarks)}</remarks>"
        "</detail></event>"
    )


class CotClient:
    """Non-blocking sender: queue + background thread, never raises to callers."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8087, max_backoff_s: float = 60.0):
        self.host, self.port = host, port
        self.max_backoff_s = max_backoff_s
        self._q: queue.Queue[str] = queue.Queue(maxsize=200)
        self._sock: socket.socket | None = None
        self._backoff = 1.0
        self._next_try = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cot", daemon=True)
        self._thread.start()
        self.connected = False

    def send(self, xml: str) -> None:
        try:
            self._q.put_nowait(xml)
        except queue.Full:
            logger.warning("CoT queue full, dropping message")

    def close(self) -> None:
        self._stop.set()
        self._drop()

    def _connect(self) -> bool:
        if time.time() < self._next_try:
            return False
        try:
            s = socket.create_connection((self.host, self.port), timeout=3)
            s.settimeout(5)
            self._sock, self.connected, self._backoff = s, True, 1.0
            logger.info("CoT connected to %s:%d", self.host, self.port)
            return True
        except OSError as e:
            self._next_try = time.time() + self._backoff
            logger.info("TAK server %s:%d unavailable (%s); retry in %.0fs", self.host, self.port, e, self._backoff)
            self._backoff = min(self._backoff * 2, self.max_backoff_s)
            return False

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock, self.connected = None, False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                xml = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            for _attempt in range(2):
                if self._sock is None and not self._connect():
                    break  # TAK down: drop this message, keep running
                try:
                    assert self._sock is not None
                    self._sock.sendall(xml.encode("utf-8"))
                    break
                except OSError as e:
                    logger.warning("CoT send failed: %s", e)
                    self._drop()
