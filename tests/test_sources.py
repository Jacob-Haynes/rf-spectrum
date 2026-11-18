import numpy as np

from rfspectrum.bands import get_preset
from rfspectrum.sources.detect import detect_hardware, pick_source
from rfspectrum.sources.rtlsdr import fft_size, hop_centres, psd_db
from rfspectrum.sources.subprocess_sweep import HackRfSweepSource, RtlPowerSource
from rfspectrum.sources.sweep_csv import SweepAssembler, parse_line


def test_parse_rtl_power_line():
    line = "2026-11-16, 12:00:00, 863000000, 865000000, 6836.00, 32, -40.1, -41.2, nan, -39.0\n"
    lo, hi, step, v = parse_line(line)
    assert lo == 863e6 and step == 6836.0
    assert list(v) == [-40.1, -41.2, -39.0]
    assert parse_line("garbage") is None


def test_hackrf_interleaved_segments_assemble_per_sweep():
    asm = SweepAssembler()
    seg = lambda lo: f"2026-11-16, 12:00:00.1, {lo}, {lo + 5_000_000}, 1000000, 20, -50, -51, -52, -53, -54\n"
    order = [2400, 2410, 2405, 2415]  # hackrf_sweep interleaving
    out = None
    for _ in range(2):
        for lo in order:
            r = asm.feed(seg(lo * 1_000_000))
            out = r or out
    assert out is not None
    f, p = out
    assert f.size == 20 and np.all(np.diff(f) > 0)
    assert f[0] == 2400.5e6


def test_commands_are_receive_only():
    pr = get_preset("5g8")
    cmd = HackRfSweepSource().command(pr)
    assert cmd[0] == "hackrf_sweep"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "5640:5950"
    rcmd = RtlPowerSource().command(get_preset("868"))
    assert rcmd[0] == "rtl_power" and rcmd[-1] == "-"


def test_hackrf_narrow_preset_padded():
    cmd = HackRfSweepSource().command(get_preset("pmr446"))
    lo, hi = map(int, cmd[cmd.index("-f") + 1].split(":"))
    assert hi - lo >= 20


def test_hop_plan_covers_preset():
    pr = get_preset("868")
    centres = hop_centres(pr)
    assert len(centres) == 4
    assert centres[0] - 0.96e6 <= pr.lo_hz and centres[-1] + 0.96e6 >= pr.hi_hz
    assert hop_centres(get_preset("pmr446")) == [446.1e6]
    assert fft_size(get_preset("pmr446")) == 16384


def test_psd_finds_tone():
    fs, n = 2.4e6, 1024
    t = np.arange(n * 8) / fs
    x = np.exp(2j * np.pi * 300e3 * t) + 0.01 * (np.random.randn(t.size) + 1j * np.random.randn(t.size))
    p = psd_db(x, n)
    k = int(np.argmax(p))
    assert abs((k - n / 2) * fs / n - 300e3) < 3 * fs / n


def test_detect_hardware_from_fake_sysfs(tmp_path):
    for name, (vid, pid) in {"1-1": ("0bda", "2838"), "1-2": ("1d50", "6089"), "1-3": ("046d", "c52b")}.items():
        d = tmp_path / name
        d.mkdir()
        (d / "idVendor").write_text(vid + "\n")
        (d / "idProduct").write_text(pid + "\n")
    assert detect_hardware(str(tmp_path)) == {"rtlsdr": True, "hackrf": True}
    assert detect_hardware(str(tmp_path / "missing")) == {"rtlsdr": False, "hackrf": False}


def test_pick_source_prefers_rtl_below_1700():
    srcs = {"rtlsdr": RtlPowerSource(), "hackrf": HackRfSweepSource()}
    assert pick_source(get_preset("868"), srcs) is srcs["rtlsdr"]
    assert pick_source(get_preset("5g8"), srcs) is srcs["hackrf"]
    assert pick_source(get_preset("5g8"), {"rtlsdr": RtlPowerSource()}) is None
