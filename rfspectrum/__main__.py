"""Entry point: python -m rfspectrum [--simulate]"""

from __future__ import annotations

import logging
import sys

import uvicorn

from rfspectrum.config import settings_from_args
from rfspectrum.runtime import Runtime
from rfspectrum.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = settings_from_args(argv)
    if sys.platform == "darwin" and settings.data_dir.startswith("/var/lib"):
        settings.data_dir = "./data"  # Mac dev convenience
    runtime = Runtime(settings)
    log = logging.getLogger("rfspectrum")
    log.info(
        "rf-spectrum (RECEIVE ONLY) mode=%s sources=%s gemini=%s",
        runtime.mode,
        ",".join(s.label for s in runtime.sources.values()),
        "on" if settings.gemini_enabled else "off",
    )
    log.info("Web UI at http://%s:%d", settings.host, settings.port)
    uvicorn.run(create_app(runtime), host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
