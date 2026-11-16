import numpy as np

from rfspectrum.bands import get_preset
from rfspectrum.simulator import AUTO_SCENARIO, FLOOR_DB, SimulatedEnvironment


def _band_max(p, preset_id, lo_mhz, hi_mhz):
    pr = get_preset(preset_id)
    f = pr.freqs_hz() / 1e6
    return p[(f >= lo_mhz) & (f <= hi_mhz)].max()


def test_quiet_floor():
    env = SimulatedEnvironment(seed=1, background=False, scenario="manual")
    p = env.render("868", 0.0)
    assert p.shape == (1024,)
    assert abs(np.median(p) - FLOOR_DB) < 3
    assert p.max() < FLOOR_DB + 6


def test_meshtastic_burst_energy_at_869525():
    env = SimulatedEnvironment(seed=1, background=False, scenario="manual")
    env.set_emitter("meshtastic", True, 0.0)
    peaks = [_band_max(env.render("868", t / 10), "868", 869.4, 869.65) for t in range(0, 60)]
    assert max(peaks) > FLOOR_DB + 20


def test_elrs_hops_across_band():
    env = SimulatedEnvironment(seed=1, background=False, scenario="manual")
    env.set_emitter("elrs868", True, 0.0)
    pr = get_preset("868")
    seen = set()
    for i in range(30):
        p = env.render("868", i / 10)
        f = pr.freqs_hz()[p > FLOOR_DB + 12] / 1e6
        seen.update(np.round(f * 2) / 2)
    assert len(seen) >= 8  # energy at many different channels


def test_vtx_only_visible_in_5g8():
    env = SimulatedEnvironment(seed=1, background=False, scenario="manual")
    env.set_emitter("vtx", True, 0.0)
    assert _band_max(env.render("5g8", 0.0), "5g8", 5795, 5805) > FLOOR_DB + 30
    assert env.render("868", 0.0).max() < FLOOR_DB + 6


def test_auto_scenario_switches_preset_and_emitters():
    env = SimulatedEnvironment(seed=1, background=False, scenario="auto")
    assert env.scenario_tick(0.0) == "868"
    assert not any(e["enabled"] for e in env.state()["emitters"] if e["group"] == "own")
    env.scenario_tick(AUTO_SCENARIO[1][0] + 0.1)
    on = {e["id"] for e in env.state()["emitters"] if e["enabled"]}
    assert "meshtastic" in on


def test_manual_toggle_leaves_auto():
    env = SimulatedEnvironment(seed=1, scenario="auto")
    env.set_emitter("vtx", True, 0.0)
    assert env.scenario == "manual"
    assert env.scenario_tick(10.0) is None


def test_first_render_at_wall_clock_time_is_fast():
    """Regression: background emitters enabled at t=0 and first rendered at wall-clock time used to
    replay ~1.8e9 s of bursts one by one while holding the render lock (minutes on a Pi), which also
    blocked POST /api/sim/emitter."""
    import time

    env = SimulatedEnvironment(seed=0, background=True, scenario="manual")
    t = time.time()
    start = time.perf_counter()
    for preset in ("868", "433", "2g4", "5g8"):
        env.render(preset, t)
    env.set_emitter("drone_flight", True, t)
    assert time.perf_counter() - start < 2.0
