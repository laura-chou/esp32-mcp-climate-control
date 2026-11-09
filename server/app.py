"""FastAPI application: the PC-side HTTP / SSE transport layer.

Routes
------
POST /api/climate        ESP32 -> PC telemetry ingest (JSON).
GET  /api/stream         PC -> MCP core / climate_agent SSE event stream.
GET  /api/fan/command    ESP32 polls (optionally long-polling) for fan commands.
POST /api/fan/command    MCP core / climate_agent pushes a fan command.
POST /api/fan/auto       Hand a device back to the rule engine.
GET  /api/health         Liveness probe.
GET  /api/status         Snapshot of devices, latest readings and fan state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .config import Settings, settings as default_settings
from .models import (
    ClimateReading,
    FanCommand,
    FanCommandRequest,
    FanState,
    IngestAck,
)
from .state import ClimateHub

logger = logging.getLogger("climate.transport")


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or default_settings
    hub = ClimateHub(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.hub = hub
        app.state.settings = settings
        logger.info(
            "climate transport layer ready on %s:%s", settings.host, settings.port
        )
        yield
        logger.info("climate transport layer shutting down")

    app = FastAPI(
        title="ESP32 Climate Control Transport Layer",
        description=(
            "HTTP ingest + SSE event stream + fan command channel for the "
            "ESP32 / MCP / Ollama climate-control system."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    # ------------------------------------------------------------------
    # Optional shared-token guard
    # ------------------------------------------------------------------
    async def require_token(
        x_device_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> None:
        expected = settings.auth_token
        if not expected:
            return
        if (x_device_token or token) != expected:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid device token",
            )

    # ------------------------------------------------------------------
    # 1. Telemetry ingest: ESP32 -> PC
    # ------------------------------------------------------------------
    @app.post(
        "/api/climate",
        response_model=IngestAck,
        status_code=status.HTTP_201_CREATED,
        summary="Ingest a DHT22 reading from the ESP32",
    )
    async def ingest_climate(
        reading: ClimateReading,
        _: None = Depends(require_token),
    ) -> IngestAck:
        envelope, command = await hub.record_reading(reading)
        current = command or hub.get_fan_command(reading.device_id)
        if current is None:
            # First reading ever: always materialise an initial OFF command.
            current = await hub.set_fan_command(
                reading.device_id, FanState.OFF, "initial state", "rule-engine"
            )
        logger.info(
            "ingest device=%s T=%.1fC H=%.1f%% -> fan=%s",
            reading.device_id,
            reading.dht22_temperature,
            reading.dht22_humidity,
            current.command.value,
        )
        return IngestAck(reading=envelope, fan_command=current)

    # ------------------------------------------------------------------
    # 2. SSE stream: PC -> MCP core / climate_agent
    # ------------------------------------------------------------------
    @app.get("/api/stream", summary="Server-Sent Events stream")
    async def stream(request: Request) -> EventSourceResponse:
        queue = hub.subscribe()

        async def event_generator() -> AsyncIterator[dict[str, Any]]:
            try:
                snapshot = {
                    "seq": hub.seq,
                    "readings": {
                        device: reading.model_dump(mode="json")
                        for device, reading in hub.latest_readings().items()
                    },
                }
                yield {"event": "snapshot", "data": json.dumps(snapshot)}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), timeout=settings.sse_keepalive_s
                        )
                    except asyncio.TimeoutError:
                        # Comment frame keeps proxies / clients from timing out.
                        yield {"comment": "keep-alive"}
                        continue
                    yield {
                        "event": payload["event"],
                        "data": json.dumps(payload["data"]),
                    }
            finally:
                hub.unsubscribe(queue)

        return EventSourceResponse(event_generator(), ping=int(settings.sse_keepalive_s))

    # ------------------------------------------------------------------
    # 3. Fan command channel: ESP32 polling / long-polling
    # ------------------------------------------------------------------
    @app.get(
        "/api/fan/command",
        response_model=FanCommand,
        summary="Poll the current fan command (long-poll with wait=1)",
    )
    async def get_fan_command(
        device_id: str = Query(default="esp32-climate-01"),
        since_seq: int = Query(default=0, ge=0),
        wait: int = Query(default=0, ge=0, le=1, description="1 enables long-polling"),
        _: None = Depends(require_token),
    ) -> FanCommand:
        if wait == 1:
            command = await hub.wait_for_fan_command(
                device_id, since_seq, settings.long_poll_timeout_s
            )
        else:
            command = hub.get_fan_command(device_id)
        if command is None:
            # Device has never reported: answer with a safe default.
            command = await hub.set_fan_command(
                device_id, FanState.OFF, "no reading yet", "rule-engine"
            )
        return command

    @app.post(
        "/api/fan/command",
        response_model=FanCommand,
        summary="Push a fan command from the MCP core / climate_agent",
    )
    async def post_fan_command(
        body: FanCommandRequest,
        _: None = Depends(require_token),
    ) -> FanCommand:
        logger.info(
            "agent command=%s device=%s reason=%s",
            body.command.value,
            body.device_id or "*",
            body.reason,
        )
        return await hub.broadcast_fan_command(
            command=body.command,
            reason=body.reason,
            source=body.source,
            device_id=body.device_id,
        )

    @app.post(
        "/api/fan/auto",
        summary="Return a device to rule-engine control",
    )
    async def reset_fan_auto(
        device_id: str = Query(default="esp32-climate-01"),
        _: None = Depends(require_token),
    ) -> dict[str, Any]:
        was_agent = hub.reset_to_auto(device_id)
        return {"status": "ok", "device_id": device_id, "was_agent_controlled": was_agent}

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @app.get("/api/health", summary="Liveness probe")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "subscribers": hub.subscriber_count,
            "devices": len(hub.latest_readings()),
            "seq": hub.seq,
        }

    @app.get("/api/status", summary="Full system snapshot")
    async def status_snapshot() -> dict[str, Any]:
        readings = {}
        for device_id, reading in hub.latest_readings().items():
            dev_status = hub.device_status(device_id)
            readings[device_id] = {
                "reading": reading.model_dump(mode="json"),
                "online": dev_status.online if dev_status else False,
                "fan_command": (
                    hub.get_fan_command(device_id).model_dump(mode="json")
                    if hub.get_fan_command(device_id)
                    else None
                ),
            }
        return {
            "settings": {
                "temp_on_c": settings.temp_on_c,
                "temp_off_c": settings.temp_off_c,
                "humidity_on": settings.humidity_on,
                "humidity_off": settings.humidity_off,
            },
            "subscribers": hub.subscriber_count,
            "devices": readings,
        }

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"status": "error", "detail": str(exc)},
        )

    return app


app = create_app()
