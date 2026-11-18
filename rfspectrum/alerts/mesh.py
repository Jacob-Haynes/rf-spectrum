"""Mesh alerts via the shared MQTT bus (`meshbus/tx`).

We never open the Meshtastic serial port: the blufor-tracker bridge owns the
radio and forwards whatever is published to meshbus/tx.

NOTE: sending a mesh alert *is* a transmission on 868 MHz. If you are
watching the 868 band, the alert itself will appear on the waterfall. That
is why mesh alerts are off by default and rate-limited.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from rfspectrum.alerts.mqttbus import MqttBus

logger = logging.getLogger(__name__)

MAX_TEXT = 199


def build_mesh_payload(text: str, to: str | None = None, channel: int = 0, source: str = "rf-spectrum") -> str:
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return json.dumps({"text": text, "to": to, "channel": channel, "source": source})


def drone_alert_text(ev: dict, simulated: bool = False) -> str:
    from rfspectrum.drone import describe

    conf = "HIGH conf" if ev.get("confidence") == "high" else "possible"
    return f"{'[SIM] ' if simulated else ''}RF: {conf} drone link: {describe(ev)}, bearing unknown"


def emitter_alert_text(emitter: dict, simulated: bool = False) -> str:
    return (
        ("[SIM] " if simulated else "")
        + f"RF: new emitter {emitter['freq_mhz']:.3f}MHz "
        f"~{emitter['bw_khz']:.0f}kHz +{emitter['peak_snr_db']:.0f}dB "
        f"({emitter['name']})"
    )


MIN_INTERVAL_S = 30.0  # contract C4.4: at most one mesh text per 30 s per tool


class MeshPublisher:
    def __init__(
        self,
        bus: MqttBus,
        topic: str = "meshbus/tx",
        to: str | None = None,
        channel: int = 0,
        min_interval_s: float = 60.0,
    ):
        self.bus = bus
        self.topic, self.to, self.channel = topic, to, channel
        self.min_interval_s = max(MIN_INTERVAL_S, min_interval_s)
        self._last = 0.0
        self._lock = threading.Lock()
        bus.start()

    def send(self, text: str) -> bool:
        """Publish a text for the mesh. Returns False if rate-limited or the broker is down."""
        with self._lock:
            now = time.time()
            if now - self._last < self.min_interval_s:
                logger.info("mesh alert rate-limited: %s", text)
                return False
            payload = build_mesh_payload(text, self.to, self.channel)
            if not self.bus.publish(self.topic, payload):
                logger.info("mesh alert not sent (MQTT not connected): %s", text)
                return False
            self._last = now
        logger.info("mesh alert published to %s", self.topic)
        return True
