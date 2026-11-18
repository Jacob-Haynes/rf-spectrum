"""EMCON debrief: offline rule-based text, optionally rewritten by Gemini.

Only the emitter summary (frequencies, bandwidths, duty cycles, rule names)
is sent to Gemini: never raw samples. Cloud mode still sends data to
Google, so use it only for demo sessions.
"""

from __future__ import annotations

import logging
import time

from rfspectrum.rules import DEFAULT_RULES, Rule

logger = logging.getLogger(__name__)


def build_summary(emitters: list[dict], emcon: dict, rules: tuple[Rule, ...] = DEFAULT_RULES) -> dict:
    by_id = {r.id: r for r in rules}
    items = []
    for e in emitters:
        if e.get("baseline"):
            continue
        rule = by_id.get(e.get("rule_id") or "")
        items.append(
            {
                "name": e["name"],
                "freq_mhz": e["freq_mhz"],
                "bw_khz": e["bw_khz"],
                "duty_pct": round(100 * e["duty"], 1),
                "active": e["active"],
                "events": e["events"],
                "likely": e.get("likely", ""),
                "advice": rule.advice if rule else "Identify this emitter: if it is yours, why is it on?",
            }
        )
    return {"emcon": emcon, "emitters": items, "generated": time.time()}


def offline_debrief(summary: dict) -> str:
    em = summary["emcon"]
    lines = [
        f"EMCON score {em['score']}/100 ({em['grade']}). "
        f"{em['emitters']} emitter(s) heard in the last {int(em.get('window_s', 60))} s, "
        f"worst duty cycle {100 * em['max_duty']:.0f}%."
    ]
    if not summary["emitters"]:
        lines.append("Nothing of ours heard. That is what good EMCON looks like on a receiver.")
        return "\n".join(lines)
    ranked = sorted(summary["emitters"], key=lambda e: -e["duty_pct"])
    for e in ranked:
        status = "ON" if e["active"] else "seen earlier"
        lines.append(
            f"- {e['name']} at {e['freq_mhz']:.3f} MHz (~{e['bw_khz']:.0f} kHz, {e['duty_pct']:.0f}% duty, {status}). "
            f"{e['advice']}"
        )
    worst = ranked[0]
    if worst["duty_pct"] >= 50:
        lines.append(
            f"Priority: {worst['name']} is transmitting most of the time. A continuous emitter is the "
            "easiest thing for an adversary to detect, fix and target."
        )
    lines.append(
        "Remember: this receiver only measured energy. An adversary with the same cheap kit "
        "plus a directional antenna can take a bearing on every one of these."
    )
    return "\n".join(lines)


def gemini_debrief(summary: dict, api_key: str, model: str) -> str:
    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = (
        "You are an instructor teaching a small team about emissions control (EMCON). "
        "A receive-only spectrum sensor (energy detection only, nothing decoded) produced the "
        "summary below during a training demo. Write a short, plain-English debrief (max 150 words): "
        "what an adversary listening would have learned, and the 3 most useful EMCON actions. "
        "Do not invent emitters that are not listed.\n\n"
        f"{summary}"
    )
    resp = client.models.generate_content(model=model, contents=prompt)
    return (resp.text or "").strip()


def debrief(summary: dict, api_key: str = "", model: str = "") -> dict:
    offline = offline_debrief(summary)
    if not api_key:
        return {"mode": "offline", "text": offline}
    try:
        text = gemini_debrief(summary, api_key, model)
        if text:
            return {"mode": f"gemini ({model})", "text": text}
    except Exception as e:
        logger.warning("Gemini debrief failed, using offline debrief: %s", e)
        return {"mode": "offline (Gemini unavailable)", "text": offline}
    return {"mode": "offline", "text": offline}
