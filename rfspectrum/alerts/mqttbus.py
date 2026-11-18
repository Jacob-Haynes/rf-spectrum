"""One persistent MQTT connection shared by the heartbeat and mesh alerts.

paho's own reconnect loop handles backoff (1 s doubling to 60 s). We publish only
while connected, and log one line per outage and one per reconnect, so a broker
that is down never floods the journal.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)


class MqttBus:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 1883,
        username: str = "guest",
        password: str = "guest",
        client_id: str | None = None,
    ):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.client_id = client_id or f"rf-spectrum-{os.getpid()}"
        self._client = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._down_logged = False

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            try:
                import paho.mqtt.client as mqtt

                c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id)
            except Exception as e:  # paho missing or too old
                logger.warning("MQTT client unavailable (%s); heartbeat and mesh alerts disabled", e)
                return
            c.username_pw_set(self.username, self.password)
            c.reconnect_delay_set(min_delay=1, max_delay=60)
            c.on_connect = self._on_connect
            c.on_disconnect = self._on_disconnect
            c.connect_async(self.host, self.port, keepalive=30)
            c.loop_start()  # paho's thread connects and reconnects with backoff
            self._client = c

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self._note_down(f"refused: {reason_code}")
            return
        self._connected.set()
        logger.info("MQTT connected to %s:%d", self.host, self.port)
        self._down_logged = False

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        was = self._connected.is_set()
        self._connected.clear()
        if was or not self._down_logged:
            self._note_down(str(reason_code))

    def _note_down(self, why: str) -> None:
        if not self._down_logged:
            logger.warning("MQTT broker %s:%d unavailable (%s); retrying with backoff", self.host, self.port, why)
            self._down_logged = True

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def publish(self, topic: str, payload: str, retain: bool = False, wait_s: float = 0.0) -> bool:
        """Publish if connected. Returns False (and drops the message) otherwise."""
        c = self._client
        if c is None or not self.connected:
            if c is not None and not self._down_logged:
                self._note_down("not connected yet")
            return False
        try:
            info = c.publish(topic, payload, qos=0, retain=retain)
            if wait_s > 0:
                info.wait_for_publish(timeout=wait_s)
            return info.rc == 0
        except Exception as e:
            logger.debug("MQTT publish to %s failed: %s", topic, e)
            return False

    def stop(self) -> None:
        with self._lock:
            c, self._client = self._client, None
        if c is None:
            return
        try:
            c.disconnect()
        except Exception:
            pass
        c.loop_stop()
        self._connected.clear()
