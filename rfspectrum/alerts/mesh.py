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
import os
import threading
import time

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


class MeshPublisher:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str = "guest",
        password: str = "guest",
        topic: str = "meshbus/tx",
        to: str | None = None,
        channel: int = 0,
        min_interval_s: float = 60.0,
    ):
        self.host, self.port = host, port
        self.auth = {"username": username, "password": password}
        self.topic, self.to, self.channel = topic, to, channel
        self.min_interval_s = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def send(self, text: str) -> bool:
        """Queue a text for the mesh. Returns False if rate-limited."""
        with self._lock:
            now = time.time()
            if now - self._last < self.min_interval_s:
                logger.info("mesh alert rate-limited: %s", text)
                return False
            self._last = now
        payload = build_mesh_payload(text, self.to, self.channel)
        threading.Thread(target=self._publish, args=(payload,), daemon=True).start()
        return True

    def _publish(self, payload: str) -> None:
        try:
            import paho.mqtt.publish as publish

            publish.single(
                self.topic, payload, qos=0, hostname=self.host, port=self.port,
                auth=self.auth, client_id=f"rf-spectrum-{os.getpid()}", keepalive=10,
            )
            logger.info("mesh alert published to %s", self.topic)
        except Exception as e:  # broker down, auth, etc.: log and carry on
            logger.warning("MQTT broker %s:%d unavailable, mesh alert not sent: %s", self.host, self.port, e)
