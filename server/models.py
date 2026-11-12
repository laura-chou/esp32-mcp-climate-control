"""Pydantic schemas shared between the ESP32 edge and the MCP core.

Field names follow the architecture spec: the ESP32 sends
``dht11_temperature`` and ``dht11_humidity`` inside a flat JSON body.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class FanState(str, Enum):
    """Fan / relay command states understood by the ESP32."""

    ON = "ON"
    OFF = "OFF"


class ClimateReading(BaseModel):
    """Incoming telemetry from the ESP32 DHT11 sensor.

    The validation window matches the DHT11 physical range (0-50 C, 20-90 %).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    dht11_temperature: float = Field(..., description="Temperature in Celsius (DHT11)")
    dht11_humidity: float = Field(..., description="Relative humidity in percent (DHT11)")

    device_id: str = Field("esp32-climate-01", description="Edge device identifier")
    timestamp: Optional[datetime] = Field(
        default=None, description="Device clock; server time is used when omitted"
    )

    @field_validator("dht11_temperature")
    @classmethod
    def _check_temperature(cls, value: float) -> float:
        if not 0.0 <= value <= 50.0:
            raise ValueError("dht11_temperature out of DHT11 range (0..50 C)")
        return value

    @field_validator("dht11_humidity")
    @classmethod
    def _check_humidity(cls, value: float) -> float:
        if not 20.0 <= value <= 90.0:
            raise ValueError("dht11_humidity out of DHT11 range (20..90 %)")
        return value


class ClimateReadingEnvelope(BaseModel):
    """A stored reading plus server-side metadata."""

    device_id: str
    dht11_temperature: float
    dht11_humidity: float
    received_at: datetime = Field(default_factory=_utcnow)
    device_timestamp: Optional[datetime] = None


class IngestAck(BaseModel):
    """Response returned to the ESP32 after a successful POST /api/climate."""

    status: str = "ok"
    reading: ClimateReadingEnvelope
    fan_command: "FanCommand"


class FanCommand(BaseModel):
    """A fan command produced by the agent / rule engine and queued for the device."""

    command: FanState
    reason: str = ""
    source: str = "rule-engine"
    issued_at: datetime = Field(default_factory=_utcnow)
    seq: int = 0

    @property
    def state_bool(self) -> bool:
        return self.command is FanState.ON


class FanCommandRequest(BaseModel):
    """Body for POST /api/fan/command, used by the MCP core / climate_agent."""

    command: FanState
    reason: str = ""
    source: str = "climate_agent"
    device_id: Optional[str] = Field(
        default=None, description="Target device; broadcast to all when omitted"
    )


IngestAck.model_rebuild()
