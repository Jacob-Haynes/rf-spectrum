"""Demo trigger contract v6: /api/demo/*, DEMO_MODE=on_demand, clear removes simulated CoT."""

import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from rfspectrum.alerts.heartbeat import build_heartbeat
from rfspectrum.config import Settings, settings_from_args
from rfspectrum.demo import SCENARIOS
from rfspectrum.frame import Frame
from rfspectrum.runtime import Runtime
from rfspectrum.web.app import create_app


def _rt(tmp_path, **kw):
    kw.setdefault("sim_background", False)
    s = Settings(backend="sim", data_dir=str(tmp_path), sim_seed=4, heartbeat_s=0, mqtt_port=1, **kw)
    return Runtime(s, start_thread=False)


def _run(rt, preset, seconds, t):
    end = t + seconds
    while t < end:
        rt.handle_frame(Frame(t, preset, rt.sim.render(preset, t)))
        t += 0.1
    return t


def _on(rt):
    return {e["id"] for e in rt.sim.state()["emitters"] if e["enabled"]}


class FakeTak:
    uid, callsign = "isrkit-rf-spectrum-test", "RF test"

    def __init__(self):
        self.sent, self.discarded, self.sa_resent = [], [], 0

    def send(self, xml, uid=None):
        self.sent.append((uid, xml))

    def discard_pending(self, uid):
        self.discarded.append(uid)

    def resend_sa(self):
        self.sa_resent += 1

    def close(self):
        pass


def test_demo_mode_env(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert settings_from_args([]).demo_mode == "continuous"
    monkeypatch.setenv("DEMO_MODE", "on_demand")
    assert settings_from_args([]).demo_mode == "on_demand"
    monkeypatch.setenv("DEMO_MODE", "nonsense")
    assert settings_from_args([]).demo_mode == "continuous"


def test_continuous_mode_is_unchanged(tmp_path):
    rt = _rt(tmp_path, sim_background=True)  # defaults: Auto demo, background emitters on
    assert rt.demo_mode == "continuous" and rt.sim.scenario == "auto"
    assert {"bg_433", "bg_868", "bg_wifi"} <= _on(rt)


def test_on_demand_starts_with_nothing_simulated(tmp_path):
    rt = _rt(tmp_path, sim_background=True, demo_mode="on_demand")
    assert rt.sim.scenario == "manual" and _on(rt) == set()
    with TestClient(create_app(rt)) as c:
        r = c.post("/api/sim/scenario", json={"scenario": "auto"})
        assert r.status_code == 409
        assert c.post("/api/sim/scenario", json={"scenario": "manual"}).status_code == 200


def test_scenarios_api_shape(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand")
    with TestClient(create_app(rt)) as c:
        d = c.get("/api/demo/scenarios").json()
        assert d["mode"] == "on_demand" and d["active"] == []
        ids = [s["id"] for s in d["scenarios"]]
        assert "fpv-flight" in ids and "meshtastic" in ids
        assert all(set(s) == {"id", "label", "hint", "duration_s"} for s in d["scenarios"])

        r = c.post("/api/demo/trigger", json={"id": "fpv-flight", "bearing": 90})  # options ignored
        body = r.json()
        assert r.status_code == 200 and body["ok"] is True and body["id"] == "fpv-flight"
        assert body["until"] > 0
        assert c.get("/api/demo/scenarios").json()["active"] == ["fpv-flight"]
        assert c.get("/api/status").json()["demo"]["active"] == ["fpv-flight"]
        assert c.post("/api/demo/trigger", json={"id": "nope"}).status_code == 404
        assert c.post("/api/demo/clear").json() == {"ok": True, "cleared": 1}
        assert c.get("/api/demo/scenarios").json()["active"] == []


def test_trigger_switches_on_and_expires(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand")
    until = rt.demo_trigger("fpv-flight", now=1000.0)
    assert until == 1000.0 + SCENARIOS["fpv-flight"].duration_s
    assert _on(rt) == {"drone_flight"} and rt.drone_watch and rt.preset_id in ("2g4", "5g8")
    rt.demo_trigger("meshtastic", now=1001.0)  # drone watch keeps sweeping; mesh joins in
    assert _on(rt) == {"drone_flight", "meshtastic"} and rt.drone_watch
    rt.demo_tick(now=until + 1)
    assert _on(rt) == set() and not rt.drone_watch and rt.demo_active() == []


def test_drone_scenarios_replace_each_other(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand")
    rt.demo_trigger("fpv-power-on", now=1000.0)
    rt.demo_trigger("fpv-flight", now=1001.0)
    assert rt.demo_active() == ["fpv-flight"] and _on(rt) == {"drone_flight"}


def test_band_scenario_tunes_its_band(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand")
    rt.demo_trigger("phone", now=1000.0)
    assert rt.preset_id == "lte800" and _on(rt) == {"phone"}


def test_continuous_trigger_takes_over_from_auto(tmp_path):
    rt = _rt(tmp_path, sim_background=True)
    rt.demo_trigger("meshtastic", now=1000.0)
    assert rt.sim.scenario == "manual" and "meshtastic" in _on(rt) and "bg_433" in _on(rt)


def test_real_sdr_has_no_scenarios(tmp_path):
    rt = _rt(tmp_path)
    rt.sim = None  # as when an SDR is attached
    with TestClient(create_app(rt)) as c:
        d = c.get("/api/demo/scenarios").json()
        assert d["scenarios"] == [] and d["available"] is False
        assert c.post("/api/demo/trigger", json={"id": "fpv-flight"}).status_code == 409
        assert c.post("/api/demo/clear").json()["cleared"] == 0


def test_clear_removes_simulated_cot_and_resets(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand", sensor_lat=52.0001, sensor_lon=0.0002)
    rt.cot = FakeTak()  # alert_cot plumbing without a socket
    rt.demo_trigger("fpv-flight", now=1000.0)
    t = _run(rt, "2g4", 15, 1000.0)
    t = _run(rt, "5g8", 15, t)
    assert rt.drone.level == "high"
    markers = [x for u, x in rt.cot.sent if u == "RFSPEC-DRONE"]
    assert markers and "SIMULATED: " in markers[-1]
    rt.cot.sent.clear()

    assert rt.demo_clear(now=t) == 1
    assert _on(rt) == set() and rt.demo_active() == [] and not rt.drone_watch
    assert "RFSPEC-DRONE" in rt.cot.discarded and rt.cot.sa_resent == 1
    xmls = [ET.fromstring(x) for _, x in rt.cot.sent]
    stale = [e for e in xmls if e.get("uid") == "RFSPEC-DRONE"]
    assert stale and stale[0].get("stale") == stale[0].get("start")  # expires at once
    dels = [e for e in xmls if e.get("type") == "t-x-d-d"]
    assert dels and dels[0].find("detail/__forcedelete") is not None
    link = dels[0].find("detail/link")
    assert link.get("uid") == "RFSPEC-DRONE" and link.get("type") == "a-u-A"
    pt = dels[0].find("point")
    assert pt.get("lat") == "52.000100" and pt.get("lon") == "0.000200"  # never 0,0
    assert "SIMULATED: " in dels[0].find("detail/remarks").text

    # Emitters the scenario added are forgotten, so a new trigger alerts and marks again.
    assert rt.drone.level == "none"
    assert not any(e["rule_id"] in ("hopper_2g4", "dji_video_5g8") for e in rt.engine.get_emitters(t))
    rt.demo_trigger("fpv-flight", now=t)
    t = _run(rt, "2g4", 15, t)
    _run(rt, "5g8", 15, t)
    assert any(u == "RFSPEC-DRONE" for u, _ in rt.cot.sent if "t-x-d-d" not in _)


def test_clear_never_touches_markers_outside_a_demo(tmp_path):
    rt = _rt(tmp_path, sensor_lat=52.0001, sensor_lon=0.0002)  # continuous, no scenario running
    rt.cot = FakeTak()
    rt.sim.set_emitter("drone_flight", True, 1000.0)
    t = _run(rt, "2g4", 15, 1000.0)
    _run(rt, "5g8", 15, t)
    assert any(u == "RFSPEC-DRONE" for u, _ in rt.cot.sent)
    rt.cot.sent.clear()
    assert rt.demo_clear(now=t) == 0 and rt.cot.sent == [] and rt.cot.sa_resent == 0


def test_heartbeat_detail_carries_demo_state(tmp_path):
    rt = _rt(tmp_path, demo_mode="on_demand")
    rt.demo_trigger("meshtastic", now=1e10)
    detail = rt.heartbeat.detail()
    assert detail == {"demo_mode": "on_demand", "demo_active": ["meshtastic"]}
    hb = build_heartbeat("simulate", "ok", 8140, detail=detail)
    assert hb["detail"]["demo_active"] == ["meshtastic"]


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_every_scenario_uses_real_sim_emitters(tmp_path, sid):
    rt = _rt(tmp_path, demo_mode="on_demand")
    rt.demo_trigger(sid, now=1000.0)
    assert set(SCENARIOS[sid].emitters) <= _on(rt)
