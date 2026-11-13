"""Runtime configuration for the transport layer.

All values can be overridden through environment variables so the same code
runs on a laptop, a home server, or inside a container without edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("CLIMATE_HOST", "0.0.0.0")
    port: int = _env_int("CLIMATE_PORT", 8801)

    # Rule-based fallback thresholds (used when the AI agent is unavailable).
    temp_on_c: float = _env_float("FAN_TEMP_ON_C", 28.0)
    temp_off_c: float = _env_float("FAN_TEMP_OFF_C", 26.0)
    humidity_on: float = _env_float("FAN_HUMIDITY_ON", 70.0)
    humidity_off: float = _env_float("FAN_HUMIDITY_OFF", 65.0)

    # Device is considered offline if no reading arrives within this window.
    device_offline_after_s: float = _env_float("DEVICE_OFFLINE_AFTER_S", 30.0)

    # After an agent-issued command, the rule engine stays out of the way for
    # this many seconds (0 disables the override entirely).
    agent_override_ttl_s: float = _env_float("AGENT_OVERRIDE_TTL_S", 60.0)

    # Long-poll waiting time for GET /api/fan/command.
    long_poll_timeout_s: float = _env_float("LONG_POLL_TIMEOUT_S", 25.0)

    # SSE keep-alive comment interval.
    sse_keepalive_s: float = _env_float("SSE_KEEPALIVE_S", 15.0)

    # Optional shared token. When set, mutating routes require it via the
    # X-Device-Token header or the `token` query parameter.
    auth_token: str | None = os.getenv("CLIMATE_AUTH_TOKEN") or None


settings = Settings()
