"""Entry point: run the transport layer with ``python main.py``.

Equivalent to:
    uvicorn server.app:app --host 0.0.0.0 --port 8801
"""

from __future__ import annotations

import logging
import os

import uvicorn

from server.config import settings

LOG_LEVEL = os.getenv("CLIMATE_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

# Our application loggers. Set the level explicitly so they are not silenced by
# a higher-level root logger in some deployments.
for _name in ("climate.transport", "climate.hub"):
    logging.getLogger(_name).setLevel(LOG_LEVEL)

logger = logging.getLogger("climate.main")


def main() -> None:
    logger.info("starting climate transport layer on %s:%s", settings.host, settings.port)
    uvicorn.run(
        "server.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
