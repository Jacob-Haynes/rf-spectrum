import numpy as np
import pytest

from rfspectrum.bands import PRESETS, get_preset
from rfspectrum.grid import resample_max


def test_presets_are_sane():
    for p in PRESETS.values():
        assert p.lo_mhz < p.hi_mhz
        assert p.freqs_hz().shape == (p.bins,)
        for a in p.allocations:
            assert p.lo_mhz <= a.lo_mhz < a.hi_mhz <= p.hi_mhz, (p.id, a.label)


def test_uk_eu_bands_present():
    assert {"433", "868", "2g4", "5g8"} <= set(PRESETS)
    labels = " ".join(a.note for a in get_preset("868").allocations)
    assert "Meshtastic" in labels


def test_hackrf_requirement():
    assert not get_preset("868").needs_hackrf()
    assert get_preset("2g4").needs_hackrf()
    assert get_preset("5g8").needs_hackrf()


def test_unknown_preset():
    with pytest.raises(KeyError):
        get_preset("nope")


def test_resample_max_keeps_narrow_peak():
    p = get_preset("868")
    f = np.linspace(p.lo_hz, p.hi_hz, 50_000)
    pw = np.full(f.size, -100.0)
    pw[25_000] = -40.0  # one fine-resolution spike
    out = resample_max(f, pw, p)
    assert out.shape == (p.bins,)
    assert out.max() == pytest.approx(-40.0)


def test_resample_interpolates_coarse_input():
    p = get_preset("868")
    f = np.linspace(p.lo_hz, p.hi_hz, 16)
    out = resample_max(f, np.linspace(-100, -90, 16), p)
    assert not np.isnan(out).any()
    assert -100.5 <= out.min() and out.max() <= -89.5
