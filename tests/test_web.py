import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from rfspectrum.config import Settings
from rfspectrum.runtime import Runtime
from rfspectrum.web.app import create_app


@pytest.fixture
def client(tmp_path):
    s = Settings(backend="sim", data_dir=str(tmp_path), sim_seed=1, sim_scenario="manual", sim_background=False,
                 mqtt_port=1)
    rt = Runtime(s)
    with TestClient(create_app(rt)) as c:
        yield c, rt


def test_index_and_status(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "Receive only" in r.text
    st = c.get("/api/status").json()
    assert st["mode"] == "simulated"
    assert st["gemini"]["enabled"] is False
    assert st["alerts"]["mesh"] is False and st["alerts"]["cot"] is False
    assert all(p["available"] for p in st["presets"])
    assert c.app.state.runtime.health() == ("simulate", "ok")


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


def test_drone_toggle_starts_drone_watch(client):
    c, rt = client
    assert rt.preset_id == "868"
    c.post("/api/sim/emitter", json={"id": "drone_flight", "on": True})
    st = c.get("/api/status").json()
    assert st["drone_watch"] and st["scan"] and rt.preset_id == "2g4"
    # the sweep alternates only between the drone bands
    rt.settings.drone_watch_dwell_s = 0
    seen = set()
    for _ in range(4):
        rt._maybe_scan()
        seen.add(rt.preset_id)
    assert seen == {"2g4", "5g8"}
    # switching to power-on keeps watching; turning the drone off ends the watch
    c.post("/api/sim/emitter", json={"id": "drone_power", "on": True})
    assert rt.drone_watch
    c.post("/api/sim/emitter", json={"id": "drone_power", "on": False})
    assert not rt.drone_watch and not rt.scan


def test_presenter_preset_choice_ends_drone_watch(client):
    c, rt = client
    c.post("/api/sim/emitter", json={"id": "drone_power", "on": True})
    assert rt.drone_watch
    c.post("/api/preset", json={"id": "868"})
    assert not rt.drone_watch and rt.preset_id == "868"


def test_drone_demo_profile_defaults_cot_on(monkeypatch):
    monkeypatch.delenv("ALERT_COT", raising=False)
    monkeypatch.setenv("RFSPEC_DEMO", "drone")
    assert Settings.from_env().alert_cot is True
    monkeypatch.setenv("ALERT_COT", "0")  # an explicit setting wins
    assert Settings.from_env().alert_cot is False
    monkeypatch.delenv("RFSPEC_DEMO")
    monkeypatch.delenv("ALERT_COT")
    assert Settings.from_env().alert_cot is False


# sha256 of the ISR kit v2 files (ui-kit/): the served copies must be the kit, unchanged.
KIT_SHA256 = {
    "isr-ui.css": "ed601e0494d7b8c93f246205d53a03916b15ceb7eb3a54526a6cd4a447879ec7",
    "isr-ui.js": "191f2b6dce0c52a760a8fdeddf4d5f127399d7d3e1684dcc26a5dbe78874d274",
    "fonts/OFL-BarlowCondensed.txt": "186d750eb496a4c17a76385f82be6aea2ac1cf2de074a811d63786cf374ea73f",
    "fonts/OFL-IBMPlexMono.txt": "7e6b2818edbd8f6a01ae80641cc8f16a51080d08fb4e532be3a0b6f74adb07da",
    "fonts/barlow-condensed-500.woff2": "31ff5e6e96a096fde4d78bc838719e69b093db2d0b062b96e423c065a6d1b2b1",
    "fonts/barlow-condensed-600.woff2": "5bc49d41f32810a114765314392332ce2acae2276635eee265a50319a12c1b49",
    "fonts/ibm-plex-mono-400.woff2": "52de70091213c9eab340e2a7ac59e7b6b2b94e089bf69ef38479b22cfde4274c",
    "fonts/ibm-plex-mono-500.woff2": "f17f51948d10b67d907393ccbac551912f2c2bd2c5af19be3aabe2f456f8fe54",
}


def test_page_uses_isr_ui_kit(client):
    c, _ = client
    html = c.get("/").text
    assert '<link rel="stylesheet" href="isr-ui.css?v=2">' in html
    assert '<script src="isr-ui.js?v=2"></script>' in html
    assert 'id="isr-status"' in html and 'id="isr-kit-link"' in html and "<h1>RF spectrum</h1>" in html
    for name, sha in KIT_SHA256.items():
        r = c.get("/" + name)
        assert r.status_code == 200
        assert hashlib.sha256(r.content).hexdigest() == sha, f"{name} differs from the ISR kit copy"
    assert c.get("/isr-ui.css?v=2").headers["content-type"].startswith("text/css")
    assert c.get("/fonts/ibm-plex-mono-400.woff2").headers["content-type"] == "font/woff2"
    assert c.get("/fonts/..%2Fapp.py").status_code == 404
    assert c.get("/fonts/missing.woff2").status_code == 404
