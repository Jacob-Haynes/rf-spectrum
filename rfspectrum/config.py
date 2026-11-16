"""Settings from environment (/etc/rf-spectrum.env) with CLI overrides."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _flag(name: str, default: bool = False) -> bool:
    v = _env(name, "1" if default else "0").lower()
    return v in ("1", "true", "yes", "on")


def parse_bands(spec: str) -> list[tuple[float, float]]:
    """'863-870,5640-5950' (MHz) -> [(863e6, 870e6), ...]"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, hi = (float(x) for x in part.split("-", 1))
        out.append((min(lo, hi) * 1e6, max(lo, hi) * 1e6))
    return out


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8140
    data_dir: str = "/var/lib/rf-spectrum"
    backend: str = "auto"  # auto | sim | rtlsdr | rtl_power | hackrf
    preset: str = "868"
    rtl_gain: str = "auto"
    rtl_ppm: int = 0
    hackrf_lna: int = 32
    hackrf_vga: int = 20
    hackrf_amp: bool = False
    threshold_db: float = 10.0
    hold_s: float = 1.0
    window_s: float = 60.0
    rules_file: str = ""
    scan_dwell_s: float = 10.0
    sim_scenario: str = "auto"
    sim_background: bool = True
    sim_seed: int | None = None
    alert_mesh: bool = False
    alert_cot: bool = False
    watch_bands: list[tuple[float, float]] = field(default_factory=list)
    alert_min_interval_s: float = 60.0
    mesh_to: str | None = None
    mesh_channel: int = 0
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_user: str = "guest"
    mqtt_pass: str = "guest"
    tak_host: str = "127.0.0.1"
    tak_port: int = 8087
    sensor_lat: float = 0.0
    sensor_lon: float = 0.0
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL

    @property
    def gemini_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @classmethod
    def from_env(cls) -> "Settings":
        seed = _env("SIM_SEED")
        return cls(
            host=_env("RFSPEC_HOST", "0.0.0.0"),
            port=int(_env("RFSPEC_PORT", "8140")),
            data_dir=_env("RFSPEC_DATA_DIR", "/var/lib/rf-spectrum"),
            backend=_env("SDR_BACKEND", "auto"),
            preset=_env("RFSPEC_PRESET", "868"),
            rtl_gain=_env("RTL_GAIN", "auto"),
            rtl_ppm=int(_env("RTL_PPM", "0")),
            hackrf_lna=int(_env("HACKRF_LNA", "32")),
            hackrf_vga=int(_env("HACKRF_VGA", "20")),
            hackrf_amp=_flag("HACKRF_AMP"),
            threshold_db=float(_env("THRESHOLD_DB", "10")),
            hold_s=float(_env("HOLD_S", "1.0")),
            window_s=float(_env("EMCON_WINDOW_S", "60")),
            rules_file=_env("RULES_FILE"),
            scan_dwell_s=float(_env("SCAN_DWELL_S", "10")),
            sim_scenario=_env("SIM_SCENARIO", "auto"),
            sim_background=_flag("SIM_BACKGROUND", True),
            sim_seed=int(seed) if seed else None,
            alert_mesh=_flag("ALERT_MESH"),
            alert_cot=_flag("ALERT_COT"),
            watch_bands=parse_bands(_env("WATCH_BANDS", "863-870")),
            alert_min_interval_s=float(_env("ALERT_MIN_INTERVAL_S", "60")),
            mesh_to=_env("MESH_TO") or None,
            mesh_channel=int(_env("MESH_CHANNEL", "0")),
            mqtt_host=_env("MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(_env("MQTT_PORT", "1883")),
            mqtt_user=_env("MQTT_USER", "guest"),
            mqtt_pass=_env("MQTT_PASS", "guest"),
            tak_host=_env("TAK_HOST", "127.0.0.1"),
            tak_port=int(_env("TAK_PORT", "8087")),
            sensor_lat=float(_env("SENSOR_LAT", "0") or 0),
            sensor_lon=float(_env("SENSOR_LON", "0") or 0),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rf-spectrum", description="Receive-only spectrum awareness / EMCON demo")
    p.add_argument("--simulate", action="store_true", help="synthetic RF environment, no SDR needed")
    p.add_argument("--backend", choices=["auto", "sim", "rtlsdr", "rtl_power", "hackrf"])
    p.add_argument("--preset", help="initial band preset (pmr446, 433, 868, 915, lte800, 2g4, 5g8)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--data-dir")
    p.add_argument("--scenario", choices=["auto", "manual"], help="simulator scenario")
    p.add_argument("--no-background", action="store_true", help="simulator: no civilian background emitters")
    p.add_argument("--seed", type=int, help="simulator RNG seed")
    return p


def settings_from_args(argv: list[str] | None = None) -> Settings:
    args = build_parser().parse_args(argv)
    s = Settings.from_env()
    if args.simulate:
        s.backend = "sim"
    if args.backend:
        s.backend = args.backend
    for name in ("preset", "host", "port", "data_dir"):
        val = getattr(args, name)
        if val is not None:
            setattr(s, name, val)
    if args.scenario:
        s.sim_scenario = args.scenario
    if args.no_background:
        s.sim_background = False
    if args.seed is not None:
        s.sim_seed = args.seed
    return s
