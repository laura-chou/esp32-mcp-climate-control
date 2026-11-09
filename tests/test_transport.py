"""Tests for the climate transport layer (real code paths, no mocks)."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from server.app import create_app
from server.config import Settings


def make_settings(**overrides) -> Settings:
    base = dict(
        temp_on_c=28.0,
        temp_off_c=26.0,
        humidity_on=70.0,
        humidity_off=65.0,
        long_poll_timeout_s=1.0,
        sse_keepalive_s=1.0,
        auth_token=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def app():
    return create_app(make_settings())


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def live_server():
    """Run a real uvicorn server so streaming responses (SSE) can be tested."""

    app = create_app(make_settings())
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn server failed to start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_ingest_returns_ack_and_rule_command(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/climate",
            json={
                "device_id": "esp32-climate-01",
                "dht22_temperature": 31.5,
                "dht22_humidity": 55.0,
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "ok"
    assert body["reading"]["dht22_temperature"] == 31.5
    # 31.5C is above the 28C ON threshold -> rule engine turns the fan ON.
    assert body["fan_command"]["command"] == "ON"
    assert body["fan_command"]["source"] == "rule-engine"


@pytest.mark.asyncio
async def test_hysteresis_keeps_fan_on_between_thresholds(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        hot = await client.post(
            "/api/climate",
            json={"dht22_temperature": 30.0, "dht22_humidity": 50.0},
        )
        assert hot.json()["fan_command"]["command"] == "ON"
        # 27C is below ON (28) but above OFF (26): must stay ON.
        warm = await client.post(
            "/api/climate",
            json={"dht22_temperature": 27.0, "dht22_humidity": 50.0},
        )
        assert warm.json()["fan_command"]["command"] == "ON"
        cool = await client.post(
            "/api/climate",
            json={"dht22_temperature": 25.0, "dht22_humidity": 50.0},
        )
        assert cool.json()["fan_command"]["command"] == "OFF"


@pytest.mark.asyncio
async def test_validation_rejects_out_of_range(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/climate",
            json={"dht22_temperature": 500.0, "dht22_humidity": 50.0},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agent_command_overrides_rule_engine(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Agent explicitly turns the fan OFF while it is hot.
        push = await client.post(
            "/api/fan/command",
            json={
                "command": "OFF",
                "reason": "agent override",
                "device_id": "esp32-climate-01",
            },
        )
        assert push.status_code == 200
        assert push.json()["source"] == "climate_agent"

        # A hot reading must NOT flip the fan back on while the agent owns it.
        resp = await client.post(
            "/api/climate",
            json={"dht22_temperature": 35.0, "dht22_humidity": 50.0},
        )
        assert resp.json()["fan_command"]["command"] == "OFF"


@pytest.mark.asyncio
async def test_fan_command_poll_and_long_poll(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(
            "/api/fan/command", params={"device_id": "dev-a", "since_seq": 0}
        )
        assert first.json()["command"] == "OFF"
        current_seq = first.json()["seq"]

        async def push_later():
            await asyncio.sleep(0.2)
            await client.post(
                "/api/fan/command",
                json={"command": "ON", "device_id": "dev-a", "reason": "test"},
            )

        task = asyncio.create_task(push_later())
        polled = await client.get(
            "/api/fan/command",
            params={"device_id": "dev-a", "since_seq": current_seq, "wait": 1},
        )
        await task
        assert polled.json()["command"] == "ON"


def test_sse_stream_emits_snapshot_then_reading(live_server):
    events: list[tuple[str, dict]] = []

    with httpx.Client(timeout=10.0) as client:
        with client.stream("GET", f"{live_server}/api/stream") as stream:
            event = "message"
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    events.append((event, json.loads(line.split(":", 1)[1].strip())))
                    if event == "snapshot":
                        # Trigger a reading once the subscriber is attached.
                        client.post(
                            f"{live_server}/api/climate",
                            json={"dht22_temperature": 29.0, "dht22_humidity": 60.0},
                        )
                    elif event == "reading":
                        break

    names = [name for name, _ in events]
    assert "snapshot" in names
    assert "reading" in names
    reading = next(data for name, data in events if name == "reading")
    assert reading["dht22_temperature"] == 29.0


@pytest.mark.asyncio
async def test_auth_token_required_when_configured():
    app = create_app(make_settings(auth_token="secret"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/api/climate",
            json={"dht22_temperature": 25.0, "dht22_humidity": 50.0},
        )
        assert denied.status_code == 401
        allowed = await client.post(
            "/api/climate",
            json={"dht22_temperature": 25.0, "dht22_humidity": 50.0},
            headers={"X-Device-Token": "secret"},
        )
        assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_health_and_status(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/climate",
            json={"dht22_temperature": 26.0, "dht22_humidity": 50.0},
        )
        health = await client.get("/api/health")
        assert health.json()["status"] == "ok"
        snapshot = await client.get("/api/status")
        assert "esp32-climate-01" in snapshot.json()["devices"]
