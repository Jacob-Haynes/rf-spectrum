#!/usr/bin/env bash
# Service entry point. Hardware is auto-detected in Python (SDR_BACKEND=auto):
# RTL-SDR and/or HackRF if present, otherwise SIMULATED mode (shown in the UI).
set -euo pipefail
cd /opt/rf-spectrum
# Prefer the RTL-SDR Blog librtlsdr in /usr/local (V4 support) over Debian's.
export LD_LIBRARY_PATH="/usr/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="/usr/local/bin:$PATH"
exec /opt/rf-spectrum/.venv/bin/python -m rfspectrum "$@"
