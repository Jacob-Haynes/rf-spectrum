import json

import pytest
from fastapi.testclient import TestClient

from rfspectrum.config import Settings
from rfspectrum.runtime import Runtime
from rfspectrum.web.app import create_app


@pytest.fixture
def client(tmp_path):
    s = Settings(backend="sim", data_dir=str(tmp_path), sim_seed=1, sim_scenario="manual", sim_background=False)
    rt = Runtime(s)
    with TestClient(create_app(rt)) as c:
        yield c, rt


def test_index_and_status(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "RECEIVE ONLY" in r.text
    st = c.get("/api/status").json()
    assert st["mode"] == "simulated"
    assert st["gemini"]["enabled"] is False
    assert st["alerts"]["mesh"] is False and st["alerts"]["cot"] is False
    assert all(p["available"] for p in st["presets"])


def test_websocket_streams_frames(client):
    c, _ = client
    with c.websocket_connect("/ws") as ws:
        first = json.loads(ws.receive_text())
        assert first["t"] == "status"
        for _ in range(20):
            msg = json.loads(ws.receive_text())
            if msg["t"] == "frame":
                assert msg["preset"] == "868" and len(msg["p"]) == 1024
                break
        else:
            pytest.fail("no frame received")


def test_controls(client):
    c, rt = client
    assert c.post("/api/preset", json={"id": "5g8"}).json()["preset"] == "5g8"
    assert c.post("/api/preset", json={"id": "bogus"}).status_code == 404
    st = c.post("/api/sim/emitter", json={"id": "vtx", "on": True}).json()
    assert any(e["id"] == "vtx" and e["enabled"] for e in st["emitters"])
    assert c.post("/api/sim/emitter", json={"id": "nope", "on": True}).status_code == 404
    assert c.post("/api/scan", json={"on": True}).json()["scan"] is True
    assert c.post("/api/baseline", json={"action": "set"}).status_code == 200
    assert c.post("/api/reset").json()["ok"] is True
    assert c.get("/api/events").json() == []


def test_offline_debrief(client):
    c, _ = client
    d = c.post("/api/debrief").json()
    assert d["mode"] == "offline"
    assert "EMCON score" in d["text"]


def test_events_logged_to_jsonl(tmp_path):
    from helpers import run_sim

    s = Settings(backend="sim", data_dir=str(tmp_path), sim_seed=1, sim_scenario="manual", sim_background=False)
    rt = Runtime(s, start_thread=False)
    run_sim("868", ["meshtastic"], 20, engine=rt.engine)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert lines and json.loads(lines[0])["rule_id"] == "meshtastic_eu"


def test_alerts_fire_only_for_new_watched_emitters(tmp_path, monkeypatch):
    from helpers import run_sim

    s = Settings(backend="sim", data_dir=str(tmp_path), alert_mesh=True, watch_bands=[(863e6, 870e6)])
    rt = Runtime(s, start_thread=False)
    sent = []
    monkeypatch.setattr(rt.mesh, "send", lambda text: sent.append(text) or True)
    run_sim("868", ["meshtastic"], 60, engine=rt.engine)
    assert len(sent) == 1 and "869.5" in sent[0]
    run_sim("5g8", ["vtx"], 10, engine=rt.engine)  # outside watched band
    assert len(sent) == 1
