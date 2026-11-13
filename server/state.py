"""In-memory state store and pub/sub hub.

The transport layer keeps the latest telemetry and fan command in memory and
broadcasts changes to SSE subscribers (the MCP core and ``climate_agent.py``).

Everything runs inside a single asyncio event loop, so the synchronisation
primitives here (``asyncio.Condition``) are sufficient; no threads or external
broker (Redis, MQTT) are required for the first iteration.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings
from .models import (
    ClimateReading,
    ClimateReadingEnvelope,
    FanCommand,
    FanState,
)
from .rules import decide_fan

logger = logging.getLogger("climate.hub")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DeviceStatus:
    device_id: str
    last_seen: datetime
    online: bool


class ClimateHub:
    """Central state + event broker shared by all HTTP routes."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._readings: dict[str, ClimateReadingEnvelope] = {}
        self._fan_commands: dict[str, FanCommand] = {}
        # Devices currently controlled by the AI agent instead of the rule engine.
        self._agent_control: dict[str, datetime] = {}

        self._seq = 0
        self._fan_condition = asyncio.Condition()

    # ------------------------------------------------------------------
    # Pub/sub for SSE
    # ------------------------------------------------------------------
    def subscribe(self, maxsize: int = 100) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        logger.info("SSE subscriber connected; total=%d", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
        logger.info("SSE subscriber disconnected; total=%d", len(self._subscribers))

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer: drop its oldest event rather than blocking.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    # ------------------------------------------------------------------
    # Telemetry ingest
    # ------------------------------------------------------------------
    async def record_reading(
        self, reading: ClimateReading
    ) -> tuple[ClimateReadingEnvelope, Optional[FanCommand]]:
        """Store an incoming reading and, in auto mode, run the rule engine."""

        envelope = ClimateReadingEnvelope(
            device_id=reading.device_id,
            dht11_temperature=reading.dht11_temperature,
            dht11_humidity=reading.dht11_humidity,
            pir_motion=reading.pir_motion,
            mq_triggered=reading.mq_triggered,
            device_timestamp=reading.timestamp,
        )
        self._readings[reading.device_id] = envelope

        await self.publish("reading", envelope.model_dump(mode="json"))

        command: Optional[FanCommand] = None
        if self._is_auto(reading.device_id):
            command = await self._apply_rule_engine(envelope)
        else:
            logger.info(
                "device=%s is under agent control; rule engine skipped",
                reading.device_id,
            )
        return envelope, command

    def control_mode(self, device_id: str) -> str:
        """Return ``"auto"`` or ``"agent"`` for the given device."""

        return "auto" if self._is_auto(device_id) else "agent"

    def _is_auto(self, device_id: str) -> bool:
        """True when the rule engine (not the agent) owns this device."""

        since = self._agent_control.get(device_id)
        if since is None:
            return True
        ttl = self._settings.agent_override_ttl_s
        if ttl > 0 and (_utcnow() - since).total_seconds() > ttl:
            self._agent_control.pop(device_id, None)
            logger.info(
                "device=%s agent override expired after %.0fs; back to auto",
                device_id,
                ttl,
            )
            return True
        return False

    async def _apply_rule_engine(
        self, envelope: ClimateReadingEnvelope
    ) -> Optional[FanCommand]:
        current = self._fan_commands.get(envelope.device_id)
        desired, reason = decide_fan(
            envelope.dht11_temperature,
            envelope.dht11_humidity,
            current.command if current else None,
            self._settings,
            pir_motion=envelope.pir_motion,
            mq_triggered=envelope.mq_triggered,
        )
        if current is not None and current.command is desired:
            return current
        logger.info(
            "rule-engine device=%s T=%.1fC H=%.1f%% motion=%s mq=%s -> %s (%s)",
            envelope.device_id,
            envelope.dht11_temperature,
            envelope.dht11_humidity,
            envelope.pir_motion,
            envelope.mq_triggered,
            desired.value,
            reason,
        )
        return await self.set_fan_command(
            device_id=envelope.device_id,
            command=desired,
            reason=reason,
            source="rule-engine",
            from_agent=False,
        )

    # ------------------------------------------------------------------
    # Fan commands
    # ------------------------------------------------------------------
    async def set_fan_command(
        self,
        device_id: str,
        command: FanState,
        reason: str = "",
        source: str = "climate_agent",
        from_agent: bool = True,
    ) -> FanCommand:
        async with self._fan_condition:
            self._seq += 1
            record = FanCommand(
                command=command,
                reason=reason,
                source=source,
                seq=self._seq,
            )
            self._fan_commands[device_id] = record
            if from_agent:
                self._agent_control[device_id] = _utcnow()
            self._fan_condition.notify_all()

        await self.publish(
            "fan_command",
            {"device_id": device_id, **record.model_dump(mode="json")},
        )
        logger.info(
            "fan command device=%s -> %s source=%s seq=%d reason=%s",
            device_id,
            record.command.value,
            record.source,
            record.seq,
            record.reason or "-",
        )
        return record

    async def broadcast_fan_command(
        self,
        command: FanState,
        reason: str = "",
        source: str = "climate_agent",
        device_id: Optional[str] = None,
    ) -> FanCommand:
        """Set a command for one device, or for every known device."""

        if device_id is not None:
            return await self.set_fan_command(device_id, command, reason, source)

        targets = set(self._readings) | set(self._fan_commands)
        if not targets:
            targets = {"esp32-climate-01"}
        logger.info(
            "broadcast fan command=%s source=%s to %d device(s): %s",
            command.value,
            source,
            len(targets),
            ", ".join(sorted(targets)),
        )
        last: Optional[FanCommand] = None
        for target in sorted(targets):
            last = await self.set_fan_command(target, command, reason, source)
        assert last is not None
        return last

    def get_fan_command(self, device_id: str) -> Optional[FanCommand]:
        return self._fan_commands.get(device_id)

    def reset_to_auto(self, device_id: str) -> bool:
        """Hand control back to the rule engine. Returns True if it was agent-owned."""

        was_agent = self._agent_control.pop(device_id, None) is not None
        logger.info(
            "device=%s reset to auto (was_agent_controlled=%s)", device_id, was_agent
        )
        return was_agent

    async def wait_for_fan_command(
        self, device_id: str, since_seq: int, timeout: float
    ) -> Optional[FanCommand]:
        """Block until a command newer than ``since_seq`` exists, or timeout."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            current = self._fan_commands.get(device_id)
            if current is not None and current.seq > since_seq:
                return current

            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.info(
                    "long-poll device=%s since_seq=%d timed out after %.0fs",
                    device_id,
                    since_seq,
                    timeout,
                )
                return current

            async with self._fan_condition:
                # Re-check while holding the lock to avoid a missed wakeup.
                current = self._fan_commands.get(device_id)
                if current is not None and current.seq > since_seq:
                    return current
                try:
                    await asyncio.wait_for(self._fan_condition.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    logger.info(
                        "long-poll device=%s since_seq=%d timed out after %.0fs",
                        device_id,
                        since_seq,
                        timeout,
                    )
                    return self._fan_commands.get(device_id)
                current = self._fan_commands.get(device_id)
                if current is not None and current.seq > since_seq:
                    logger.info(
                        "long-poll device=%s woken by seq=%d (was_seq=%d)",
                        device_id,
                        current.seq,
                        since_seq,
                    )
                    return current

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def latest_readings(self) -> dict[str, ClimateReadingEnvelope]:
        return dict(self._readings)

    def latest_reading(self, device_id: str) -> Optional[ClimateReadingEnvelope]:
        return self._readings.get(device_id)

    def device_status(self, device_id: str) -> Optional[DeviceStatus]:
        reading = self._readings.get(device_id)
        if reading is None:
            return None
        age = (_utcnow() - reading.received_at).total_seconds()
        return DeviceStatus(
            device_id=device_id,
            last_seen=reading.received_at,
            online=age <= self._settings.device_offline_after_s,
        )

    @property
    def seq(self) -> int:
        return self._seq
