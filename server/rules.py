"""Rule-based fallback for the fan decision.

The AI agent (``climate_agent.py``) is the primary decision maker. This module
only provides a deterministic fallback so the system still behaves sensibly
when Ollama / the agent is offline, and so a fresh checkout demonstrates an
end-to-end loop without any model installed.

Hysteresis is used to avoid rapid ON/OFF flapping around a single threshold.
"""

from __future__ import annotations

from .config import Settings
from .models import FanState


def decide_fan(
    temperature_c: float,
    humidity: float,
    current: FanState | None,
    settings: Settings,
) -> tuple[FanState, str]:
    """Return the desired fan state and a human-readable reason."""

    if current is FanState.ON:
        cool_enough = temperature_c <= settings.temp_off_c
        dry_enough = humidity <= settings.humidity_off
        if cool_enough and dry_enough:
            return FanState.OFF, (
                f"temperature {temperature_c:.1f}C <= {settings.temp_off_c}C and "
                f"humidity {humidity:.1f}% <= {settings.humidity_off}%"
            )
        return FanState.ON, "still above the OFF threshold (hysteresis)"

    too_hot = temperature_c >= settings.temp_on_c
    too_humid = humidity >= settings.humidity_on
    if too_hot or too_humid:
        trigger = "temperature" if too_hot else "humidity"
        return FanState.ON, (
            f"{trigger} crossed threshold "
            f"(T={temperature_c:.1f}C, H={humidity:.1f}%)"
        )
    return FanState.OFF, "within comfortable range"
