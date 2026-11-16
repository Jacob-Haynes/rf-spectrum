import xml.etree.ElementTree as ET

from rfspectrum.alerts.cot import build_drone_cot
from rfspectrum.alerts.mesh import drone_alert_text
from rfspectrum.config import Settings
from rfspectrum.drone import DroneLinkDetector
from rfspectrum.frame import Frame
from rfspectrum.runtime import Runtime
from rfspectrum.simulator import AUTO_SCENARIO, SimulatedEnvironment

from helpers import run_sim


def _rt(tmp_path, **kw):
    s = Settings(backend="sim", data_dir=str(tmp_path), sim_seed=4, sim_scenario="manual", heartbeat_s=0, **kw)
    return Runtime(s, start_thread=False)


def _run(rt, preset, seconds, t):
    end = t + seconds
    while t < end:
        rt.handle_frame(Frame(t, preset, rt.sim.render(preset, t)))
        t += 0.1
    return t


def test_power_on_is_possible_then_video_makes_it_high(tmp_path):
    rt = _rt(tmp_path, sim_background=False)
    t = _run(rt, "2g4", 10, 1000.0)
    assert rt.drone.level == "none"
    rt.sim.set_emitter("drone_power", True, t)
    t = _run(rt, "2g4", 20, t)
    assert rt.drone.level == "possible"
    assert [c["rule_id"] for c in rt.drone.components] == ["hopper_2g4"]
    t = _run(rt, "5g8", 20, t)  # 2.4 GHz C2 keeps its last-known state while we look at 5.8
    assert rt.drone.level == "high"
    video = [c for c in rt.drone.components if c["rule_id"] == "dji_video_5g8"]
    assert video and 8000 <= video[0]["bw_khz"] <= 25000 and 5725 <= video[0]["freq_mhz"] <= 5850
    types = [e["type"] for e in rt.engine.get_events() if e["type"].startswith("drone")]
    assert types == ["drone", "drone"]  # possible, then high (newest first)
    assert all(e["simulated"] for e in rt.engine.get_events())


def test_flight_video_is_wider_and_drone_toggles_are_exclusive():
    env = SimulatedEnvironment(seed=1, background=False, scenario="manual")
    env.set_emitter("drone_power", True, 0)
    env.set_emitter("drone_flight", True, 0)
    on = {e["id"] for e in env.state()["emitters"] if e["enabled"]}
    assert "drone_flight" in on and "drone_power" not in on
    eng, _, t = run_sim("5g8", [], 15, env=env)
    em = [e for e in eng.get_emitters(now=t) if e["rule_id"] == "dji_video_5g8"]
    assert em and 15000 <= em[0]["bw_khz"] <= 30000 and em[0]["duty"] > 0.8
    assert not any(e["rule_id"] == "vtx_5g8" for e in eng.get_emitters(now=t))


def test_analog_vtx_still_analog_not_dji():
    eng, _, t = run_sim("5g8", ["vtx"], 15)
    assert {e["rule_id"] for e in eng.get_emitters(now=t)} == {"vtx_5g8"}


def test_background_wifi_alone_does_not_trigger_drone(tmp_path):
    rt = _rt(tmp_path, sim_background=True)
    _run(rt, "2g4", 60, 1000.0)
    assert rt.drone.level == "none"


def test_baselined_emitters_ignored():
    det = DroneLinkDetector()
    em = {"rule_id": "dji_video_5g8", "active": True, "baseline": True, "duty": 1.0, "freq_mhz": 5795, "bw_khz": 20000}
    assert det.evaluate([em], 0) is None
    em["baseline"] = False
    ev = det.evaluate([em], 1)
    assert ev["confidence"] == "possible" and ev["escalation"]


def test_alert_texts_are_labelled_simulated():
    ev = {"type": "drone", "name": "Possible drone C2/video link", "confidence": "possible",
          "components": [{"short": "drone video link 5.8 GHz"}]}
    txt = drone_alert_text(ev, simulated=True)
    assert txt.startswith("[SIM] ") and "drone video link 5.8 GHz" in txt and len(txt) < 200
    assert not drone_alert_text(ev).startswith("[SIM]")
    root = ET.fromstring(build_drone_cot(ev, 51.1, -1.7, simulated=True))
    assert root.get("type") == "a-u-A"
    remarks = root.find("detail/remarks").text
    assert remarks.startswith("SIMULATED: RF: drone video link 5.8 GHz, bearing unknown")


def test_drone_alerts_use_mesh_and_cot_plumbing(tmp_path, monkeypatch):
    rt = _rt(tmp_path, sim_background=False, alert_mesh=True)
    sent = []
    monkeypatch.setattr(rt.mesh, "send", lambda text: sent.append(text) or True)
    rt.sim.set_emitter("drone_flight", True, 1000.0)
    t = _run(rt, "2g4", 20, 1000.0)
    _run(rt, "5g8", 20, t)
    drone_txt = [s for s in sent if "drone link" in s]
    assert len(drone_txt) == 2 and all(s.startswith("[SIM]") for s in drone_txt)
    assert "HIGH" in drone_txt[-1]


def test_auto_scenario_includes_drone_segment():
    owns = [a.get("own", []) for _, a in AUTO_SCENARIO]
    assert any("drone_power" in o for o in owns) and any("drone_flight" in o for o in owns)
