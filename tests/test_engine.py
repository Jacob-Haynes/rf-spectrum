import numpy as np

from rfspectrum.bands import get_preset
from rfspectrum.engine import Engine, NoiseFloor, emcon_score, find_hits
from rfspectrum.frame import Frame

from helpers import run_sim


def test_quiet_band_has_no_events():
    eng, events, t = run_sim("868", [], 30)
    assert events == []
    em = eng.get_emcon(now=t)
    assert em["score"] == 100 and em["grade"] == "SILENT"


def test_meshtastic_burst_detected_with_duration():
    eng, events, t = run_sim("868", ["meshtastic"], 40)
    starts = [e for e in events if e["type"] == "start"]
    ends = [e for e in events if e["type"] == "end"]
    assert starts and starts[0]["rule_id"] == "meshtastic_eu"
    assert starts[0]["new_emitter"] is True
    assert all(not e["new_emitter"] for e in starts[1:])
    assert abs(starts[0]["freq_mhz"] - 869.525) < 0.05
    assert 150 <= starts[0]["bw_khz"] <= 350
    assert ends and ends[0]["duration_s"] > 0.3
    em = eng.get_emcon(now=t)
    assert em["emitters"] == 1 and em["max_duty"] < 0.3


def test_elrs_hopper_is_one_emitter_with_high_duty():
    eng, events, t = run_sim("868", ["elrs868"], 20)
    emitters = eng.get_emitters(now=t)
    assert [e["rule_id"] for e in emitters] == ["hopper_868"]
    assert emitters[0]["duty"] > 0.7
    assert eng.get_emcon(now=t)["grade"] in ("ELEVATED", "LOUD")


def test_vtx_is_continuous_and_loud():
    eng, events, t = run_sim("5g8", ["vtx"], 20)
    emitters = eng.get_emitters(now=t)
    assert [e["rule_id"] for e in emitters] == ["vtx_5g8"]
    assert emitters[0]["duty"] > 0.9
    assert abs(emitters[0]["freq_mhz"] - 5800) < 2
    assert eng.get_emcon(now=t)["grade"] == "LOUD"


def test_vtx_not_absorbed_into_noise_floor():
    eng, events, t = run_sim("5g8", ["vtx"], 180)
    assert eng.get_emitters(now=t)[0]["duty"] > 0.9


def test_phone_and_pmr_and_433():
    eng, _, t = run_sim("lte800", ["phone"], 30)
    assert "lte800_ul" in {e["rule_id"] for e in eng.get_emitters(now=t)}
    eng, _, t = run_sim("pmr446", ["pmr446"], 30)
    assert "pmr446" in {e["rule_id"] for e in eng.get_emitters(now=t)}
    eng, _, t = run_sim("433", [], 90, background=True)
    assert "srd_433" in {e["rule_id"] for e in eng.get_emitters(now=t)}


def test_2g4_hopper_and_wifi_without_unclassified_noise():
    eng, _, t = run_sim("2g4", ["phone", "elrs2g4"], 30, background=True)
    rules = {e["rule_id"] for e in eng.get_emitters(now=t)}
    assert {"hopper_2g4", "wifi_2g4"} <= rules
    assert None not in rules  # no unclassified fragments


def test_all_own_kit_off_but_background_on_then_baseline():
    eng, events, t = run_sim("868", [], 120, background=True, seed=2)
    assert eng.get_emcon(now=t)["seen_total"] >= 1  # the civilian alarm sensor
    eng.set_baseline()
    em = eng.get_emcon(now=t)
    assert em["emitters"] == 0 and em["score"] == 100 and em["baseline"] >= 1


def test_score_formula():
    assert emcon_score([])["score"] == 100
    one = emcon_score([{"duty": 0.1}])
    assert one["score"] == 80 and one["grade"] == "LOW"
    assert emcon_score([{"duty": 1.0}, {"duty": 0.5}])["grade"] == "LOUD"


def test_noise_floor_tracks_noise_and_clamps():
    nf = NoiseFloor()
    rng = np.random.default_rng(0)
    for _ in range(100):
        p = -100 + rng.normal(0, 0.5, 512)
        p[200:260] = -50  # strong continuous carrier
        floor = nf.update(p)
    assert abs(np.median(floor) - (-100)) < 2
    assert floor[230] < -94  # carrier never becomes "noise"


def test_find_hits_bandwidth():
    preset = get_preset("868")
    p = np.full(preset.bins, -100.0)
    p[500:537] = -70.0  # ~250 kHz block
    hits = find_hits(p, np.full(preset.bins, -100.0), preset, 10)
    assert len(hits) == 1
    assert abs(hits[0].bw_hz - 37 * preset.bin_hz) < 1
    assert hits[0].snr_db == 30


def test_unclassified_emitter():
    preset = get_preset("433")
    eng = Engine()
    t = 0.0
    for i in range(60):
        p = np.full(preset.bins, -100.0) + np.random.default_rng(i).normal(0, 0.3, preset.bins)
        if i > 10:
            p[50:54] = -60  # 432.9 MHz: amateur sub-band, no rule
        eng.process(Frame(t, "433", p.astype(np.float32)))
        t += 0.1
    ems = eng.get_emitters(now=t)
    assert len(ems) == 1 and ems[0]["rule_id"] is None and ems[0]["name"].startswith("Unclassified")


def test_unmonitored_band_keeps_last_known_state():
    eng, _, t = run_sim("868", ["elrs868"], 20)
    # switch to a different band for 90 s: the hopper is no longer observed
    eng, _, t2 = run_sim("433", [], 90, engine=eng, t0=t)
    ems = {e["rule_id"]: e for e in eng.get_emitters(now=t2)}
    assert ems["hopper_868"]["monitored"] is False
    assert ems["hopper_868"]["active"] is True and ems["hopper_868"]["duty"] > 0.7


def test_event_callback_errors_do_not_break_engine():
    def boom(ev, em):
        raise RuntimeError("alert failed")

    eng, events, _ = run_sim("868", ["meshtastic"], 20, engine=Engine(on_event=boom))
    assert events
