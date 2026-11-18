"""Long-lived CoT client for OpenTAKServer (contract v5). Standard library only.

OpenTAKServer 1.8.x only accepts CoT from a registered end-user device, so a
one-shot connection per alert never reaches ATAK. This client therefore:

  C5.1  keeps ONE TCP connection to TAK_HOST:TAK_PORT, reconnecting with backoff;
  C5.2  on every (re)connect waits `register_delay_s` (3 s: OTS binds a client's
        queue only if its first <contact> arrives after its async channel opens),
        then sends a registration SA with a STABLE uid and callsign (OTS silently
        refuses a callsign already held by another uid), re-sent every 60 s;
  C5.3  sends every marker on that same connection, only after registration.

Markers keep their own uids. At most one message per 5 s per uid is sent; inside
that window only the newest is kept and it goes out when the window opens.
"""

from __future__ import annotations

import collections
import logging
import platform
import select
import socket
import threading
import time
from datetime import datetime, timezone
from xml.sax.saxutils import quoteattr

logger = logging.getLogger(__name__)

REGISTER_DELAY_S = 3.0
SA_INTERVAL_S = 60.0
SA_STALE_S = 120.0
MIN_UID_INTERVAL_S = 5.0
MAX_QUEUE = 200


def iso_z(ts: float) -> str:
    """ISO 8601 UTC with milliseconds, ending in Z."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_registration_sa(
    station: str,
    lat: float | None,
    lon: float | None,
    now: float | None = None,
    version: str = "0.1.0",
    tool: str = "rf-spectrum",
) -> str:
    """Registration SA for this sensor as an EUD. Unset position -> never-drawn 0,0 point."""
    now = time.time() if now is None else now
    uid = f"isrkit-{tool}-{station}"
    callsign = f"RF {station}"
    start = iso_z(now)
    if lat is None or lon is None:
        # The only 0,0 allowed in the kit: huge error, stale immediately, so nothing draws it.
        point = '<point lat="0" lon="0" hae="9999999" ce="9999999" le="9999999"/>'
        stale = start
    else:
        point = f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="0" ce="50" le="9999999"/>'
        stale = iso_z(now + SA_STALE_S)
    os_name = f"{platform.system()} {platform.release()}".strip()
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(uid)} type="a-f-G-E-S" how="m-g"'
        f' time="{start}" start="{start}" stale="{stale}">'
        f"{point}"
        "<detail>"
        f'<contact callsign={quoteattr(callsign)} endpoint="*:-1:stcp"/>'
        '<__group name="Cyan" role="Team Member"/>'
        f'<takv platform="isr-kit" device={quoteattr(tool)} os={quoteattr(os_name)} version={quoteattr(version)}/>'
        "</detail></event>"
    )


class TakClient:
    """Non-blocking sender: callers queue XML; one background thread owns the socket."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8087,
        station: str = "localhost",
        lat: float | None = None,
        lon: float | None = None,
        version: str = "0.1.0",
        register_delay_s: float = REGISTER_DELAY_S,
        sa_interval_s: float = SA_INTERVAL_S,
        min_uid_interval_s: float = MIN_UID_INTERVAL_S,
        max_backoff_s: float = 60.0,
    ):
        self.host, self.port = host, port
        self.station, self.lat, self.lon, self.version = station, lat, lon, version
        self.uid = f"isrkit-rf-spectrum-{station}"
        self.callsign = f"RF {station}"
        self.register_delay_s = register_delay_s
        self.sa_interval_s = sa_interval_s
        self.min_uid_interval_s = min_uid_interval_s
        self.max_backoff_s = max_backoff_s

        self._q: collections.deque[str] = collections.deque()
        self._qlock = threading.Lock()
        self._last_uid: dict[str, float] = {}
        self._pending: dict[str, str] = {}  # uid -> newest throttled message
        self._sock: socket.socket | None = None
        self._connected_at = 0.0
        self._last_sa = 0.0
        self.registered = False
        self._backoff = 1.0
        self._next_try = 0.0
        self._down_logged = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tak", daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ------------------------------------------------------------ public

    def send(self, xml: str, uid: str | None = None) -> None:
        with self._qlock:
            if uid:
                now = time.time()
                if now - self._last_uid.get(uid, 0.0) < self.min_uid_interval_s:
                    self._pending[uid] = xml
                    return
                self._last_uid[uid] = now
            self._push(xml)
        self._wake.set()

    def discard_pending(self, uid: str) -> None:
        """Forget a throttled message for uid, so it can't go out after a deletion."""
        with self._qlock:
            self._pending.pop(uid, None)

    def resend_sa(self) -> None:
        """Re-send the registration SA soon (OTS marks us disconnected after a t-x-d-d)."""
        self._last_sa = 0.0
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=3)
        self._drop()

    # ------------------------------------------------------------ internals

    def _push(self, xml: str) -> None:  # caller holds _qlock
        if len(self._q) >= MAX_QUEUE:
            self._q.popleft()
            logger.warning("TAK queue full, dropping oldest message")
        self._q.append(xml)

    def _flush_pending(self) -> None:
        now = time.time()
        with self._qlock:
            for u in [u for u in self._pending if now - self._last_uid.get(u, 0.0) >= self.min_uid_interval_s]:
                self._push(self._pending.pop(u))
                self._last_uid[u] = now

    def _connect(self) -> bool:
        if time.time() < self._next_try:
            return False
        try:
            s = socket.create_connection((self.host, self.port), timeout=3)
            s.settimeout(5)
        except OSError as e:
            self._next_try = time.time() + self._backoff
            if not self._down_logged:
                logger.warning("TAK server %s:%d unavailable (%s); retrying with backoff", self.host, self.port, e)
                self._down_logged = True
            self._backoff = min(self._backoff * 2, self.max_backoff_s)
            return False
        self._sock, self._backoff, self._down_logged = s, 1.0, False
        self._connected_at, self.registered = time.time(), False
        logger.info("TAK connected to %s:%d; registering as %s in %.0f s",
                    self.host, self.port, self.callsign, self.register_delay_s)
        return True

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            logger.info("TAK connection to %s:%d closed", self.host, self.port)
        self._sock, self.registered = None, False

    def _write(self, xml: str) -> bool:
        assert self._sock is not None
        try:
            self._sock.sendall(xml.encode("utf-8"))
            return True
        except OSError as e:
            logger.warning("TAK send failed: %s", e)
            self._drop()
            return False

    def _drain(self) -> None:
        """Read and discard what the server sends us, and notice when it hangs up."""
        while self._sock is not None:
            try:
                r, _, _ = select.select([self._sock], [], [], 0)
                if not r:
                    return
                if not self._sock.recv(65536):
                    self._drop()
            except OSError:
                self._drop()

    def _send_sa(self) -> bool:
        ok = self._write(build_registration_sa(self.station, self.lat, self.lon, version=self.version))
        if ok:
            self._last_sa = time.time()
        return ok

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._sock is None and not self._connect():
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            self._drain()
            if self._sock is None:
                continue
            now = time.time()
            if not self.registered:
                wait = self._connected_at + self.register_delay_s - now
                if wait > 0:
                    self._stop.wait(min(wait, 0.1))
                    continue
                if self._send_sa():
                    self.registered = True
                    logger.info("TAK registered as %s (%s)", self.callsign, self.uid)
                continue
            if now - self._last_sa >= self.sa_interval_s and not self._send_sa():
                continue
            self._flush_pending()
            with self._qlock:
                xml = self._q.popleft() if self._q else None
            if xml is None:
                self._wake.wait(0.1)
                self._wake.clear()
                continue
            if not self._write(xml):
                with self._qlock:
                    self._q.appendleft(xml)  # resend after re-registering
