"""CoT marker builders. Sending is done by alerts.tak_client.TakClient (one registered connection).

Energy detection gives no bearing or range, so the marker is placed at the
sensor's own position (SENSOR_LAT/SENSOR_LON) with a large CE and the
frequency details in the remarks.
"""

from __future__ import annotations

import time
from xml.sax.saxutils import escape, quoteattr

from rfspectrum.alerts.tak_client import iso_z as _iso

STALE_S = 300


def build_drone_cot(
    ev: dict, lat: float, lon: float, simulated: bool = False, now: float | None = None
) -> str:
    """Unknown-air marker at the sensor position for a drone link signature."""
    from rfspectrum.drone import describe

    now = time.time() if now is None else now
    tag = "SIMULATED: " if simulated else ""
    remarks = (
        f"{tag}RF: {describe(ev)}, bearing unknown. {ev['name']}. "
        "Energy detection only; marker is at the sensor, not the drone."
    )
    callsign = ("SIM " if simulated else "") + "RF drone link"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{DRONE_UID}" type="a-u-A" how="m-g"'
        f' time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(now + STALE_S)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="2000" le="9999999"/>'
        "<detail>"
        f"<contact callsign={quoteattr(callsign)}/>"
        f"<remarks>{escape(remarks)}</remarks>"
        "</detail></event>"
    )


DRONE_UID = "RFSPEC-DRONE"


def emitter_uid(emitter: dict, uid_prefix: str = "RFSPEC") -> str:
    return f"{uid_prefix}-{emitter['key'].replace(':', '-')}"


def build_emitter_cot(
    emitter: dict, lat: float, lon: float, now: float | None = None, uid_prefix: str = "RFSPEC",
    simulated: bool = False,
) -> str:
    now = time.time() if now is None else now
    uid = emitter_uid(emitter, uid_prefix)
    callsign = ("SIM " if simulated else "") + f"RF {emitter['freq_mhz']:.3f}"
    remarks = (
        ("SIMULATED: " if simulated else "")
        + f"RF emitter (energy detection only, position = sensor): {emitter['name']}. "
        f"{emitter['freq_mhz']:.3f} MHz, ~{emitter['bw_khz']:.0f} kHz wide, "
        f"peak {emitter['peak_snr_db']:.0f} dB over noise. Likely: {emitter.get('likely', '')}"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(uid)} type="a-u-G" how="m-g"'
        f' time="{_iso(now)}" start="{_iso(now)}" stale="{_iso(now + STALE_S)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="1000" le="9999999"/>'
        "<detail>"
        f"<contact callsign={quoteattr(callsign)}/>"
        f"<remarks>{escape(remarks)}</remarks>"
        "</detail></event>"
    )
