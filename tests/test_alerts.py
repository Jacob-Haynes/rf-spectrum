import json
import logging
import socket
import threading
import time
import xml.etree.ElementTree as ET

from rfspectrum.alerts.cot import build_emitter_cot
from rfspectrum.alerts.heartbeat import TOPIC, Heartbeat, build_heartbeat, short_hostname
from rfspectrum.alerts.mesh import MeshPublisher, build_mesh_payload, emitter_alert_text
from rfspectrum.alerts.mqttbus import MqttBus

EMITTER = {
    "key": "meshtastic_eu", "name": "LoRa 869.4-869.65 (Meshtastic EU)", "freq_mhz": 869.525,
    "bw_khz": 250.0, "peak_snr_db": 30.0, "likely": "Meshtastic <node> & co",
}


def test_cot_xml_is_valid():
    xml = build_emitter_cot(EMITTER, 52.0, 0.1, now=1_800_000_000)
    root = ET.fromstring(xml)
    assert root.tag == "event" and root.get("type") == "a-u-G"
    assert root.get("uid") == "RFSPEC-meshtastic_eu"
    assert root.find("point").get("lat") == "52.000000"
    assert "869.525" in root.find("detail/remarks").text


def test_mesh_payload_contract():
    payload = json.loads(build_mesh_payload(emitter_alert_text(EMITTER)))
    assert set(payload) == {"text", "to", "channel", "source"}
    assert payload["to"] is None and payload["channel"] == 0 and payload["source"] == "rf-spectrum"
    assert len(payload["text"]) < 200
    assert len(json.loads(build_mesh_payload("x" * 500))["text"]) < 200


class FakeBroker:
    """Minimal MQTT 3.1.1 broker: CONNACKs every client, records PUBLISH (topic, payload, retain)."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.got: list[tuple[str, str, bool]] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    def _serve(self, conn):
        try:
            while True:
                head = self._read(conn, 1)[0]
                length, mult = 0, 1
                while True:
                    b = self._read(conn, 1)[0]
                    length += (b & 0x7F) * mult
                    mult *= 128
                    if not b & 0x80:
                        break
                body = self._read(conn, length) if length else b""
                kind = head >> 4
                if kind == 1:  # CONNECT
                    conn.sendall(b"\x20\x02\x00\x00")
                elif kind == 3:  # PUBLISH (qos 0)
                    tl = int.from_bytes(body[:2], "big")
                    self.got.append((body[2:2 + tl].decode(), body[2 + tl:].decode(), bool(head & 1)))
                elif kind == 12:  # PINGREQ
                    conn.sendall(b"\xd0\x00")
                elif kind == 14:  # DISCONNECT
                    return
        except OSError:
            return
        finally:
            conn.close()

    def close(self):
        self.srv.close()


def test_mesh_rate_limit_and_30s_floor():
    class Bus:
        def __init__(self):
            self.sent = []

        def start(self):
            pass

        def publish(self, topic, payload, retain=False, wait_s=0.0):
            self.sent.append((topic, payload))
            return True

    bus = Bus()
    pub = MeshPublisher(bus, min_interval_s=5)
    assert pub.min_interval_s == 30  # contract C4.4: never more than 1 per 30 s
    assert pub.send("test") is True
    assert pub.send("again") is False  # rate-limited
    assert len(bus.sent) == 1 and bus.sent[0][0] == "meshbus/tx"


def test_mesh_broker_down_is_harmless_and_quiet(caplog):
    bus = MqttBus(port=1)
    pub = MeshPublisher(bus, min_interval_s=60)
    with caplog.at_level(logging.DEBUG):
        time.sleep(1.5)  # paho retries in the background
        assert pub.send("test") is False  # not connected: dropped, not queued
        assert pub.send("test") is False
    bus.stop()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) <= 1, [r.getMessage() for r in warnings]
    assert not any("not connected" in r.getMessage().lower() and r.name.startswith("paho") for r in caplog.records)


def test_bus_heartbeat_and_mesh_share_one_connection(caplog):
    broker = FakeBroker()
    bus = MqttBus(port=broker.port)
    hb = Heartbeat(lambda: ("simulate", "ok"), 8140, bus, interval_s=0.2)
    pub = MeshPublisher(bus, min_interval_s=60)
    with caplog.at_level(logging.INFO):
        hb.start()
        assert bus.wait_connected(3)
        time.sleep(0.5)
        assert pub.send("[SIM] RF: hello") is True
        time.sleep(0.2)
        hb.stop()
        bus.stop()
    broker.close()
    beats = [json.loads(p) for t, p, r in broker.got if t == "isrkit/heartbeat/rf-spectrum" and r]
    assert len(beats) >= 2 and beats[-1]["status"] == "stopped" and beats[0]["status"] == "ok"
    mesh = [json.loads(p) for t, p, _ in broker.got if t == "meshbus/tx"]
    assert mesh and mesh[0]["text"] == "[SIM] RF: hello"
    noisy = [r for r in caplog.records if "not connected" in r.getMessage().lower()]
    assert noisy == []


def test_heartbeat_payload_contract(monkeypatch):
    monkeypatch.delenv("ISR_HOST", raising=False)
    hb = build_heartbeat("simulate", "ok", 8140, now=1_800_000_000.7)
    assert TOPIC == "isrkit/heartbeat/rf-spectrum"
    assert set(hb) == {"tool", "host", "url", "mode", "status", "ts"}
    assert hb["tool"] == "rf-spectrum" and hb["ts"] == 1_800_000_000
    assert hb["url"] == f"http://{short_hostname()}.local:8140" and "." not in hb["host"]
    assert hb["host"] == hb["host"].lower()
    assert build_heartbeat("cpu", "ok", 8140, public_url="http://isr-pi.local:8140")["url"] == "http://isr-pi.local:8140"


def test_heartbeat_host_from_isr_host(monkeypatch):
    monkeypatch.setenv("ISR_HOST", "isr-droneops")
    hb = build_heartbeat("simulate", "ok", 8140)
    assert hb["host"] == "isr-droneops" and hb["url"] == "http://isr-droneops.local:8140"
    monkeypatch.setattr(socket, "gethostname", lambda: "ISR-Pi.local")
    monkeypatch.delenv("ISR_HOST")
    assert short_hostname() == "isr-pi"


def test_heartbeat_broker_down_is_harmless():
    bus = MqttBus(port=1)
    hb = Heartbeat(lambda: ("simulate", "ok"), 8140, bus, interval_s=0.1)
    hb.start()
    time.sleep(0.3)
    hb.stop()  # "stopped" is dropped quietly: not connected
    bus.stop()


def test_sensor_position_unset_or_zero_is_none(monkeypatch):
    from rfspectrum.config import Settings

    for lat, lon in (("", ""), ("0", "0"), ("51.1", ""), ("x", "1"), ("95", "1")):
        monkeypatch.setenv("SENSOR_LAT", lat)
        monkeypatch.setenv("SENSOR_LON", lon)
        s = Settings.from_env()
        assert s.sensor_lat is None and s.sensor_lon is None, (lat, lon)
    monkeypatch.setenv("SENSOR_LAT", "52.0001")
    monkeypatch.setenv("SENSOR_LON", "0.0002")
    s = Settings.from_env()
    assert (s.sensor_lat, s.sensor_lon) == (52.0001, 0.0002)


def test_cli_overrides_mqtt_and_tak():
    from rfspectrum.config import settings_from_args

    s = settings_from_args(["--simulate", "--mqtt-host", "192.168.4.1", "--mqtt-user", "isrkit",
                            "--tak-host", "192.168.4.1", "--tak-port", "8088"])
    assert (s.mqtt_host, s.mqtt_user, s.tak_host, s.tak_port, s.host) == ("192.168.4.1", "isrkit", "192.168.4.1", 8088, "0.0.0.0")
