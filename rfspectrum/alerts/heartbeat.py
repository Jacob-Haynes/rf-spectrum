"""Retained MQTT heartbeat on isrkit/heartbeat/rf-spectrum so the Pi launcher can show us."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Callable

from rfspectrum.alerts.mqttbus import MqttBus

TOPIC = "isrkit/heartbeat/rf-spectrum"


def short_hostname() -> str:
    """Contract C4.1: ISR_HOST if set, else the lowercased short hostname."""
    env = os.environ.get("ISR_HOST", "").strip()
    if env:
        return env
    return (socket.gethostname().split(".")[0] or "localhost").lower()


def build_heartbeat(mode: str, status: str, port: int, public_url: str = "", now: float | None = None) -> dict:
    host = short_hostname()
    return {
        "tool": "rf-spectrum",
        "host": host,
        "url": public_url or f"http://{host}.local:{port}",
        "mode": mode,  # simulate | cpu
        "status": status,  # ok | degraded | error | stopped
        "ts": int(time.time() if now is None else now),
    }


class Heartbeat:
    def __init__(
        self,
        state: Callable[[], tuple[str, str]],  # -> (mode, status)
        port: int,
        bus: MqttBus | None = None,
        interval_s: float = 10.0,
        public_url: str = "",
    ):
        self.state = state
        self.port, self.public_url = port, public_url
        self.bus = bus
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval_s > 0 and self.bus is not None and self._thread is None:
            self.bus.start()
            self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3)
        self._thread = None
        mode, _ = self.state()
        self._publish(build_heartbeat(mode, "stopped", self.port, self.public_url), wait_s=2.0)

    def _run(self) -> None:
        assert self.bus is not None
        self.bus.wait_connected(3.0)  # first beat as soon as the broker answers
        while not self._stop.is_set():
            mode, status = self.state()
            self._publish(build_heartbeat(mode, status, self.port, self.public_url))
            self._stop.wait(self.interval_s)

    def _publish(self, payload: dict, wait_s: float = 0.0) -> bool:
        if self.bus is None:
            return False
        return self.bus.publish(TOPIC, json.dumps(payload), retain=True, wait_s=wait_s)
