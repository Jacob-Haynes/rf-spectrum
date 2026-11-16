"""Drone link signature: correlate emitters that look like an FPV control or video link.

Energy and occupancy only, nothing decoded:
  C2     many short narrowband bursts hopping across 2400-2480 MHz (ELRS 2.4)
  video  persistent wideband block in 5725-5850 MHz (DJI O3/O4-style, 10/20/40 MHz),
         an analog 5.8 GHz VTX carrier, or (weaker evidence) a near-continuous
         wideband 2.4 GHz block
One component -> "possible drone C2/video link"; C2 + video -> higher confidence.

Bluetooth also hops at 2.4 GHz and a busy Wi-Fi AP looks like a 2.4 GHz block, which
is why one component alone is only "possible". Set an RF baseline before the drone
powers up to exclude the local environment.
"""

from __future__ import annotations

# rule_id: (UI label, short alert text, minimum duty cycle)
C2_RULES = {"hopper_2g4": ("ELRS-style 2.4 GHz hopping control link", "drone control link 2.4 GHz", 0.15)}
VIDEO_RULES = {
    "dji_video_5g8": ("digital video link 5.8 GHz (DJI O3/O4-style)", "drone video link 5.8 GHz", 0.5),
    "vtx_5g8": ("analog video link 5.8 GHz", "drone video link 5.8 GHz", 0.5),
    "wifi_2g4": ("possible video link 2.4 GHz (or busy Wi-Fi)", "possible drone video link 2.4 GHz", 0.9),
}
LEVELS = {"none": 0, "possible": 1, "high": 2}


class DroneLinkDetector:
    def __init__(self):
        self.level = "none"
        self.components: list[dict] = []
        self.since: float | None = None

    def _match(self, emitters: list[dict], table: dict) -> list[dict]:
        out = []
        for e in emitters:
            spec = table.get(e.get("rule_id") or "")
            if spec and e["active"] and not e.get("baseline") and e["duty"] >= spec[2]:
                out.append({
                    "label": spec[0], "short": spec[1], "rule_id": e["rule_id"], "freq_mhz": e["freq_mhz"],
                    "bw_khz": e["bw_khz"], "duty": e["duty"],
                })
        return out

    def evaluate(self, emitters: list[dict], now: float) -> dict | None:
        """Returns an event dict when the confidence level changes, else None."""
        c2 = self._match(emitters, C2_RULES)
        video = self._match(emitters, VIDEO_RULES)
        level = "high" if c2 and video else "possible" if (c2 or video) else "none"
        self.components = c2 + video
        if level == self.level:
            return None
        prev, self.level = self.level, level
        if level == "none":
            self.since = None
            return {"type": "drone_end", "ts": now, "name": "Drone link signature cleared",
                    "confidence": "none", "components": [], "previous": prev}
        if LEVELS[level] < LEVELS[prev]:
            return {"type": "drone", "ts": now, "name": self.title(), "confidence": level,
                    "components": self.components, "escalation": False}
        self.since = self.since or now
        return {"type": "drone", "ts": now, "name": self.title(), "confidence": level,
                "components": self.components, "escalation": True}

    def title(self) -> str:
        if self.level == "high":
            return "Drone C2 + video link (higher confidence)"
        if self.level == "possible":
            return "Possible drone C2/video link"
        return ""

    def state(self) -> dict:
        return {"level": self.level, "title": self.title(), "since": self.since,
                "components": self.components}


def describe(ev: dict) -> str:
    """Short alert text, e.g. 'drone video link 5.8 GHz + drone control link 2.4 GHz'."""
    seen: list[str] = []
    for c in ev.get("components", []):
        if c["short"] not in seen:
            seen.append(c["short"])
    return " + ".join(seen) or "drone link"
