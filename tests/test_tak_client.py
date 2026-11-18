"""TakClient against a fake OpenTAKServer that enforces the three OTS 1.8.3 rules:

1. CoT from a connection that hasn't registered as an EUD is dropped.
2. A connection whose FIRST <contact> event arrives sooner than `bind_after_s` after
   connect is never bound, so nothing it sends is delivered.
3. A registration whose callsign is already held by a different uid is refused.

It also applies OTS's marker *storage* rules (C5.5) to delivered events, see `storable`.

Timings are scaled down (bind_after_s=0.3) so the suite stays fast.
"""

import re
import socket
import threading
import time
import xml.etree.ElementTree as ET

from rfspectrum.alerts.cot import build_drone_cot
from rfspectrum.alerts.tak_client import TakClient, build_registration_sa

BIND = 0.3


class FakeOTS:
    def __init__(self, bind_after_s=BIND):
        self.bind_after_s = bind_after_s
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.callsigns: dict[str, str] = {}  # callsign -> uid
        self.delivered: list[ET.Element] = []
        self.dropped: list[tuple[str, str]] = []  # (reason, uid)
        self.registrations: list[str] = []
        self.stored: list[ET.Element] = []
        self.conns: list[socket.socket] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._serve, args=(c, time.time()), daemon=True).start()

    def _serve(self, conn, t0):
        state = {"uid": None, "unbound": False}
        buf = b""
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                while b"</event>" in buf:
                    raw, buf = buf.split(b"</event>", 1)
                    raw = raw[raw.find(b"<event"):] + b"</event>"
                    self._handle(ET.fromstring(raw), state, time.time() - t0)
        except OSError:
            return

    def _handle(self, ev, state, age):
        uid = ev.get("uid")
        contact = ev.find("detail/contact")
        with self.lock:
            if state["uid"] is None and not state["unbound"] and contact is not None:
                if age < self.bind_after_s:  # rule 2
                    state["unbound"] = True
                    self.dropped.append(("contact-too-early", uid))
                    return
                cs = contact.get("callsign")
                owner = self.callsigns.get(cs)
                if owner is not None and owner != uid:  # rule 3
                    state["unbound"] = True
                    self.dropped.append(("duplicate-callsign", uid))
                    return
                self.callsigns[cs] = uid
                state["uid"] = uid
                self.registrations.append(uid)
                self.delivered.append(ev)
                return
            if state["uid"] is None:  # rule 1
                self.dropped.append(("unregistered-sender", uid))
                return
            self.delivered.append(ev)
            if storable(ev, set(self.registrations)):
                self.stored.append(ev)

    def marker_uids(self):
        with self.lock:
            return [e.get("uid") for e in self.delivered if e.get("type") != "a-f-G-E-S"]

    def kick_all(self):
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def close(self):
        self.kick_all()
        self.srv.close()


STORABLE_TYPE = re.compile(r"^a-[fhupansjk]-[ZPAGSUF]|^b-m-p")


def storable(ev, eud_uids) -> bool:
    """OTS 1.8.3 stores a marker only if all of these hold."""
    t = ev.get("type", "")
    if not STORABLE_TYPE.match(t) or t.startswith("b-m-p-s-p-loc") or t == "a-f-G-E-S":
        return False
    if ev.find("detail/takv") is not None:
        return False
    contact = ev.find("detail/contact")
    if contact is not None and contact.get("endpoint") is not None:
        return False
    pt = ev.find("point")
    if pt is None or (float(pt.get("lat")) == 0 and float(pt.get("lon")) == 0):
        return False
    return all(link.get("uid") in eud_uids for link in ev.findall("detail/link"))


DRONE_EV = {"type": "drone", "name": "Possible drone C2/video link", "confidence": "possible",
            "components": [{"short": "drone DJI/digital video link 5.8 GHz"}]}


def _wait(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _client(ots, **kw):
    kw.setdefault("register_delay_s", BIND + 0.1)
    return TakClient("127.0.0.1", ots.port, kw.pop("station", "pi1"), 52.0, 0.1, **kw)


def test_one_shot_sender_is_dropped_by_fake_ots():
    ots = FakeOTS()
    with socket.create_connection(("127.0.0.1", ots.port)) as s:  # old behaviour: connect, send, close
        s.sendall(build_drone_cot(DRONE_EV, 52.0, 0.1, simulated=True).encode())
    assert _wait(lambda: ots.dropped)
    assert ots.delivered == []
    ots.close()


def test_registers_after_delay_then_markers_are_delivered_on_same_connection():
    ots = FakeOTS()
    c = _client(ots)
    c.send(build_drone_cot(DRONE_EV, 52.0, 0.1, simulated=True), uid="RFSPEC-DRONE")  # queued pre-registration
    assert _wait(lambda: ots.marker_uids() == ["RFSPEC-DRONE"])
    assert ots.registrations == ["isrkit-rf-spectrum-pi1"] and ots.dropped == []
    assert len(ots.conns) == 1  # C5.1: one connection, not one per alert
    assert c.registered
    c.close()
    ots.close()


def test_registering_too_early_is_never_bound():
    ots = FakeOTS()
    c = _client(ots, register_delay_s=0.0)  # what a naive client does
    c.send(build_drone_cot(DRONE_EV, 52.0, 0.1), uid="RFSPEC-DRONE")
    assert _wait(lambda: ("contact-too-early", "isrkit-rf-spectrum-pi1") in ots.dropped)
    time.sleep(0.3)
    assert ots.marker_uids() == []
    c.close()
    ots.close()


def test_duplicate_callsign_under_new_uid_is_refused_but_stable_uid_reregisters():
    ots = FakeOTS()
    c = _client(ots)
    assert _wait(lambda: ots.registrations == ["isrkit-rf-spectrum-pi1"])
    # an impostor using our callsign with another uid is refused (rule 3)
    with socket.create_connection(("127.0.0.1", ots.port)) as s:
        time.sleep(BIND + 0.05)
        sa = build_registration_sa("pi1", 52.0, 0.1).replace("isrkit-rf-spectrum-pi1", "random-uid-123")
        s.sendall(sa.encode())
        assert _wait(lambda: ("duplicate-callsign", "random-uid-123") in ots.dropped)
    # the server drops us; we reconnect, re-register with the SAME uid and are accepted again
    ots.kick_all()
    assert _wait(lambda: ots.registrations.count("isrkit-rf-spectrum-pi1") == 2, timeout=6)
    c.send(build_drone_cot(DRONE_EV, 52.0, 0.1), uid="RFSPEC-DRONE")
    assert _wait(lambda: "RFSPEC-DRONE" in ots.marker_uids())
    assert not any(r == "duplicate-callsign" and u.startswith("isrkit-") for r, u in ots.dropped)
    c.close()
    ots.close()


def test_sa_is_resent_periodically():
    ots = FakeOTS()
    c = _client(ots, sa_interval_s=0.3)
    assert _wait(lambda: sum(e.get("type") == "a-f-G-E-S" for e in ots.delivered) >= 3)
    c.close()
    ots.close()


def test_markers_throttled_per_uid_keep_newest():
    ots = FakeOTS()
    c = _client(ots, min_uid_interval_s=0.5)
    for i in range(3):
        c.send(build_drone_cot(dict(DRONE_EV, name=f"v{i}"), 52.0, 0.1), uid="RFSPEC-DRONE")
    c.send(build_drone_cot(DRONE_EV, 52.0, 0.1).replace("RFSPEC-DRONE", "RFSPEC-other"), uid="RFSPEC-other")
    assert _wait(lambda: len(ots.marker_uids()) == 3)
    with ots.lock:
        remarks = [e.find("detail/remarks").text for e in ots.delivered if e.get("uid") == "RFSPEC-DRONE"]
    assert len(remarks) == 2 and "v0" in remarks[0] and "v2" in remarks[1]  # v1 superseded
    c.close()
    ots.close()


def test_tak_down_is_harmless_and_logs_once(caplog):
    import logging

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with caplog.at_level(logging.INFO):
        c = TakClient("127.0.0.1", port, "pi1", None, None, max_backoff_s=0.2)
        c.send("<event/>", uid="x")
        time.sleep(1.0)
        c.close()
    assert not c.connected
    assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 1


def test_drone_and_emitter_markers_are_stored_by_ots():
    from rfspectrum.alerts.cot import build_emitter_cot

    emitter = {"key": "meshtastic_eu", "name": "LoRa (Meshtastic EU)", "freq_mhz": 869.525,
               "bw_khz": 250.0, "peak_snr_db": 30.0, "likely": "Meshtastic"}
    ots = FakeOTS()
    c = _client(ots)
    for sim in (True, False):
        parent = {"parent_uid": c.uid, "parent_callsign": c.callsign}
        c.send(build_drone_cot(DRONE_EV, 52.0, 0.1, simulated=sim, **parent))
        c.send(build_emitter_cot(emitter, 52.0, 0.1, simulated=sim, **parent))
    assert _wait(lambda: len(ots.marker_uids()) == 4)
    with ots.lock:
        stored = [(e.get("uid"), e.get("type")) for e in ots.stored]
        link = ots.stored[0].find("detail/link")
    assert stored.count(("RFSPEC-DRONE", "a-u-A")) == 2
    assert stored.count(("RFSPEC-meshtastic_eu", "a-u-G")) == 2
    assert link.get("uid") == "isrkit-rf-spectrum-pi1" and link.get("relation") == "p-p"
    c.close()
    ots.close()


def test_storability_rules_reject_what_ots_rejects():
    from rfspectrum.alerts.cot import build_emitter_cot

    eud = {"isrkit-rf-spectrum-pi1"}
    good = ET.fromstring(build_drone_cot(DRONE_EV, 52.0, 0.1, parent_uid="isrkit-rf-spectrum-pi1"))
    assert storable(good, eud)
    # our markers never carry these
    assert good.find("detail/takv") is None and good.find("detail/contact").get("endpoint") is None
    # a link to another marker's uid is not storable
    bad_link = ET.fromstring(build_drone_cot(DRONE_EV, 52.0, 0.1, parent_uid="RFSPEC-meshtastic_eu"))
    assert not storable(bad_link, eud)
    # the registration SA itself (takv + endpoint) is not a stored marker
    assert not storable(ET.fromstring(build_registration_sa("pi1", 52.0, 0.1)), eud)
    # no position: the builders refuse to produce a marker at all
    assert build_drone_cot(DRONE_EV, None, None) is None
    assert build_drone_cot(DRONE_EV, 0.0, 0.0) is None
    assert build_emitter_cot({"key": "k", "name": "n", "freq_mhz": 1, "bw_khz": 1, "peak_snr_db": 1}, 0, 0) is None


ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def test_registration_sa_contract():
    root = ET.fromstring(build_registration_sa("isr-pi", 52.0, 0.1, now=1_800_000_000, version="0.1.0"))
    assert root.get("uid") == "isrkit-rf-spectrum-isr-pi" and root.get("type") == "a-f-G-E-S"
    for k in ("time", "start", "stale"):
        assert ISO_Z.match(root.get(k)), root.get(k)
    assert root.get("stale") == "2027-01-15T08:02:00.000Z"  # now + 120 s
    contact = root.find("detail/contact")
    assert contact.get("callsign") == "RF isr-pi" and contact.get("endpoint") == "*:-1:stcp"
    grp = root.find("detail/__group")
    assert (grp.get("name"), grp.get("role")) == ("Cyan", "Team Member")
    takv = root.find("detail/takv")
    assert takv.get("platform") == "isr-kit" and takv.get("device") == "rf-spectrum" and takv.get("version") == "0.1.0"
    assert takv.get("os")
    assert root.find("point").get("lat") == "52.000000"


def test_registration_sa_without_position_is_never_drawn():
    root = ET.fromstring(build_registration_sa("isr-pi", None, None, now=1_800_000_000))
    pt = root.find("point")
    assert (pt.get("lat"), pt.get("lon")) == ("0", "0")
    assert pt.get("hae") == pt.get("ce") == pt.get("le") == "9999999"
    assert root.get("stale") == root.get("start")


def test_runtime_uses_stable_station_identity(tmp_path, monkeypatch):
    from rfspectrum.config import Settings
    from rfspectrum.runtime import Runtime

    monkeypatch.setenv("ISR_HOST", "isr-droneops")
    rt = Runtime(Settings(backend="sim", data_dir=str(tmp_path), alert_cot=True, tak_port=1, heartbeat_s=0,
                          sim_scenario="manual"), start_thread=False)
    assert rt.cot.uid == "isrkit-rf-spectrum-isr-droneops" and rt.cot.callsign == "RF isr-droneops"
    rt.cot.close()
