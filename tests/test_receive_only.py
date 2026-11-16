"""Guard rail: the code base must never grow transmit, jamming, replay or decode paths."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = [
    r"hackrf_transfer",
    r"\bset_tx\w*",
    r"\btx_gain\b",
    r"\bwrite_samples\b",
    r"\bSOAPY_SDR_TX\b",
    r"\bjam(m(er|ing))?\b",
    r"\breplay\b",
    r"\bdemod(ulate)?\w*\(",
]


def _code_files():
    for p in list((ROOT / "rfspectrum").rglob("*.py")) + list((ROOT / "deploy").glob("*.sh")):
        yield p


def test_no_transmit_or_decode_code():
    hits = []
    for p in _code_files():
        text = p.read_text()
        # allow the explicit warnings/comments that say we never do these things
        code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
        code = re.sub(r'""".*?"""', "", code, flags=re.S)
        for pat in FORBIDDEN:
            if re.search(pat, code, flags=re.I):
                hits.append(f"{p.relative_to(ROOT)}: {pat}")
    assert not hits, hits
