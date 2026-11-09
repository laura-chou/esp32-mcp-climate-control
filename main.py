"""Entry point: run the transport layer with ``python main.py``.

Equivalent to:
    uvicorn server.app:app --host 0.0.0.0 --port 8801
"""

from __future__ import annotations

import logging

import uvicorn

from server.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


def main() -> None:
    uvicorn.run(
        "server.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
