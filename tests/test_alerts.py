import json
import socket
import threading
import time
import xml.etree.ElementTree as ET

from rfspectrum.alerts.cot import CotClient, build_emitter_cot
from rfspectrum.alerts.mesh import MeshPublisher, build_mesh_payload, emitter_alert_text

EMITTER = {
    "key": "meshtastic_eu", "name": "LoRa 869.4-869.65 (Meshtastic EU)", "freq_mhz": 869.525,
    "bw_khz": 250.0, "peak_snr_db": 30.0, "likely": "Meshtastic <node> & co",
}


def test_cot_xml_is_valid():
    xml = build_emitter_cot(EMITTER, 51.1, -1.75, now=1_800_000_000)
    root = ET.fromstring(xml)
    assert root.tag == "event" and root.get("type") == "a-u-G"
    assert root.get("uid") == "RFSPEC-meshtastic_eu"
    assert root.find("point").get("lat") == "51.100000"
    assert "869.525" in root.find("detail/remarks").text


def test_cot_client_survives_tak_down():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listening
    c = CotClient("127.0.0.1", port)
    c.send(build_emitter_cot(EMITTER, 0, 0))
    time.sleep(0.5)
    assert c.connected is False
    c.close()


def test_cot_client_delivers():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def accept():
        conn, _ = srv.accept()
        conn.settimeout(3)
        got.append(conn.recv(65536))
        conn.close()

    th = threading.Thread(target=accept, daemon=True)
    th.start()
    c = CotClient("127.0.0.1", srv.getsockname()[1])
    c.send(build_emitter_cot(EMITTER, 0, 0))
    th.join(5)
    c.close()
    srv.close()
    assert got and b"<event" in got[0]


def test_mesh_payload_contract():
    payload = json.loads(build_mesh_payload(emitter_alert_text(EMITTER)))
    assert set(payload) == {"text", "to", "channel", "source"}
    assert payload["to"] is None and payload["channel"] == 0 and payload["source"] == "rf-spectrum"
    assert len(payload["text"]) < 200
    assert len(json.loads(build_mesh_payload("x" * 500))["text"]) < 200


def test_mesh_broker_down_is_harmless_and_rate_limited():
    pub = MeshPublisher(port=1, min_interval_s=60)
    assert pub.send("test") is True
    assert pub.send("again") is False  # rate-limited
    time.sleep(0.3)  # background publish fails quietly
