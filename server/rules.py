"""Rule-based fallback for the fan decision.

The AI agent (``climate_agent.py``) is the primary decision maker. This module
only provides a deterministic fallback so the system still behaves sensibly
when Ollama / the agent is offline, and so a fresh checkout demonstrates an
end-to-end loop without any model installed.

It is multimodal:

* **MQ-135** -- a triggered air-quality reading overrides everything and forces
  the fan ON so the room is ventilated.
* **PIR** -- when nobody is present the fan is kept off for comfort-only reasons
  (energy saving), while an active heat/humidity trigger still wins.
* **Temperature / humidity** -- hysteresis avoids rapid ON/OFF flapping around a
  single threshold.
"""

from __future__ import annotations

from .config import Settings
from .models import FanState


def decide_fan(
    temperature_c: float,
    humidity: float,
    current: FanState | None,
    settings: Settings,
    pir_motion: bool = True,
    mq_triggered: bool = False,
) -> tuple[FanState, str]:
    """Return the desired fan state and a human-readable reason.

    ``pir_motion`` defaults to True (assume occupied) so callers without PIR
    information keep the original climate-only behaviour.
    """

    # 1. Air quality first: bad air is ventilated regardless of comfort.
    if mq_triggered:
        return FanState.ON, (
            f"MQ-135 air quality threshold exceeded -> ventilate "
            f"(T={temperature_c:.1f}C, H={humidity:.1f}%)"
        )

    # 2. Climate hysteresis.
    if current is FanState.ON:
        cool_enough = temperature_c <= settings.temp_off_c
        dry_enough = humidity <= settings.humidity_off
        if cool_enough and dry_enough:
            if not pir_motion:
                return FanState.OFF, (
                    f"cooled to T={temperature_c:.1f}C<={settings.temp_off_c}C and "
                    f"H={humidity:.1f}%<={settings.humidity_off}% while unoccupied "
                    f"-> energy saving"
                )
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
    if not pir_motion:
        return FanState.OFF, "within comfortable range and unoccupied"
    return FanState.OFF, "within comfortable range"
