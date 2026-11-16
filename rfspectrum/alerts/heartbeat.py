"""Retained MQTT heartbeat on isrkit/heartbeat/rf-spectrum so the Pi launcher can show us."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

TOPIC = "isrkit/heartbeat/rf-spectrum"


def short_hostname() -> str:
    return socket.gethostname().split(".")[0] or "localhost"


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
        host: str = "127.0.0.1",
        mqtt_port: int = 1883,
        username: str = "guest",
        password: str = "guest",
        interval_s: float = 10.0,
        public_url: str = "",
    ):
        self.state = state
        self.port, self.public_url = port, public_url
        self.host, self.mqtt_port = host, mqtt_port
        self.auth = {"username": username, "password": password}
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False

    def start(self) -> None:
        if self.interval_s > 0 and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=3)
        mode, _ = self.state()
        self._publish(build_heartbeat(mode, "stopped", self.port, self.public_url))

    def _run(self) -> None:
        while not self._stop.is_set():
            mode, status = self.state()
            self._publish(build_heartbeat(mode, status, self.port, self.public_url))
            self._stop.wait(self.interval_s)

    def _publish(self, payload: dict) -> None:
        try:
            import paho.mqtt.publish as publish

            publish.single(
                TOPIC, json.dumps(payload), qos=0, retain=True, hostname=self.host, port=self.mqtt_port,
                auth=self.auth, client_id=f"rf-spectrum-hb-{os.getpid()}", keepalive=10,
            )
            self._warned = False
        except Exception as e:  # broker down: log once, keep trying quietly
            log = logger.debug if self._warned else logger.warning
            log("heartbeat to MQTT %s:%d failed: %s", self.host, self.mqtt_port, e)
            self._warned = True
