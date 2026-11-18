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


DRONE_UID = "RFSPEC-DRONE"
DRONE_TYPE = "a-u-A"  # unknown air
EMITTER_TYPE = "a-u-G"  # unknown ground
SA_TYPE = "a-f-G-E-S"  # the type of our own registration SA (the parent EUD)


def _marker(
    uid: str, cot_type: str, callsign: str, lat: float | None, lon: float | None, ce: float,
    remarks: str, now: float, parent_uid: str | None = None, parent_callsign: str | None = None,
    stale_s: float = STALE_S,
) -> str | None:
    """A marker OpenTAKServer both relays and stores (OTS 1.8.3 rules, contract C5.5):
    type matches ^a-[fhupansjk]-[ZPAGSUF]; no <takv>; <contact> has a callsign but no
    endpoint; a real position (never 0,0); any <link uid> is our own EUD registration uid.
    Returns None without a real position. stale_s=0 expires the marker at once."""
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    t = _iso(now)
    link = ""
    if parent_uid:
        link = f'<link uid={quoteattr(parent_uid)} relation="p-p" type="{SA_TYPE}" production_time="{t}"'
        if parent_callsign:
            link += f" parent_callsign={quoteattr(parent_callsign)}"
        link += "/>"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(uid)} type={quoteattr(cot_type)} how="m-g"'
        f' time="{t}" start="{t}" stale="{_iso(now + stale_s)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="{ce:.0f}" le="9999999"/>'
        "<detail>"
        f"<contact callsign={quoteattr(callsign)}/>"
        f"<remarks>{escape(remarks)}</remarks>"
        f"{link}"
        "</detail></event>"
    )


def build_drone_cot(
    ev: dict, lat: float | None, lon: float | None, simulated: bool = False, now: float | None = None,
    parent_uid: str | None = None, parent_callsign: str | None = None, stale_s: float = STALE_S,
) -> str | None:
    """Unknown-air marker at the sensor position for a drone link signature."""
    from rfspectrum.drone import describe

    tag = "SIMULATED: " if simulated else ""
    remarks = (
        f"{tag}RF: {describe(ev)}, bearing unknown. {ev['name']}. "
        "Energy detection only; marker is at the sensor, not the drone."
    )
    callsign = ("SIM " if simulated else "") + "RF drone link"
    return _marker(DRONE_UID, DRONE_TYPE, callsign, lat, lon, 2000, remarks,
                   time.time() if now is None else now, parent_uid, parent_callsign, stale_s)


def emitter_uid(emitter: dict, uid_prefix: str = "RFSPEC") -> str:
    return f"{uid_prefix}-{emitter['key'].replace(':', '-')}"


def build_emitter_cot(
    emitter: dict, lat: float | None, lon: float | None, now: float | None = None, uid_prefix: str = "RFSPEC",
    simulated: bool = False, parent_uid: str | None = None, parent_callsign: str | None = None,
    stale_s: float = STALE_S,
) -> str | None:
    callsign = ("SIM " if simulated else "") + f"RF {emitter['freq_mhz']:.3f}"
    remarks = (
        ("SIMULATED: " if simulated else "")
        + f"RF emitter (energy detection only, position = sensor): {emitter['name']}. "
        f"{emitter['freq_mhz']:.3f} MHz, ~{emitter['bw_khz']:.0f} kHz wide, "
        f"peak {emitter['peak_snr_db']:.0f} dB over noise. Likely: {emitter.get('likely', '')}"
    )
    return _marker(emitter_uid(emitter, uid_prefix), EMITTER_TYPE, callsign, lat, lon, 1000, remarks,
                   time.time() if now is None else now, parent_uid, parent_callsign, stale_s)


def build_delete_cot(
    target_uid: str, target_type: str, lat: float | None, lon: float | None, now: float | None = None,
    simulated: bool = True,
) -> str | None:
    """Remove a marker from ATAK maps: t-x-d-d linking to it, with <__forcedelete/>.

    OpenTAKServer 1.8.3 relays this but keeps its own stored copy, so send the marker once
    more with stale_s=0 as well. OTS stores the point against the sender, so it is the
    sensor position, never 0,0 (None without one), and it marks the sender disconnected
    until its next registration SA, so re-send that afterwards."""
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    now = time.time() if now is None else now
    t = _iso(now)
    remarks = ("SIMULATED: " if simulated else "") + "RF: demo cleared, marker removed."
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(f"{target_uid}-del-{int(now * 1000)}")} type="t-x-d-d"'
        f' how="h-g-i-g-o" time="{t}" start="{t}" stale="{_iso(now + 10)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="9999999" le="9999999"/>'
        "<detail>"
        f'<link uid={quoteattr(target_uid)} relation="none" type={quoteattr(target_type)}/>'
        "<__forcedelete/>"
        f"<remarks>{escape(remarks)}</remarks>"
        "</detail></event>"
    )
