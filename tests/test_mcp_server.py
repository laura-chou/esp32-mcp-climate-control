"""Tests for the MCP server wrapper (real code paths, real HTTP, no mocks).

Each test drives the MCP server over the SDK's in-memory transport while the
tools talk to a real uvicorn instance of the FastAPI transport layer, so the full
round trip (MCP -> HTTP -> ClimateHub) is exercised.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from core_logic import TransportClient
from mcp_server import build_server
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


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _start_server(settings: Settings) -> str:
    app = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn server failed to start"
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def live_server():
    yield _start_server(make_settings())


@pytest.fixture
def token_server():
    yield _start_server(make_settings(auth_token="s3cret"))


@contextlib.asynccontextmanager
async def open_session(base_url: str, token: str | None = None):
    """Yield an initialized MCP ClientSession bound to a live transport layer.

    The in-memory transport is entered and exited inside the caller's task so the
    SDK's internal task group stays balanced (an async pytest fixture would tear
    it down from a different task).
    """

    client = TransportClient(base_url=base_url, token=token)
    server = build_server(client=client)
    try:
        async with InMemoryTransport(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    finally:
        client.close()


def _structured(result) -> dict:
    """Return a tool result's structured content as a plain dict."""

    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_lists_tools_and_resources(live_server):
    async with open_session(live_server) as session:
        tools = {t.name for t in (await session.list_tools()).tools}
        assert {
            "get_system_status",
            "get_latest_reading",
            "set_fan_state",
            "reset_fan_auto",
            "poll_fan_command",
            "get_recent_events",
        } <= tools

        resources = {str(r.uri) for r in (await session.list_resources()).resources}
        assert "climate://status" in resources
        assert "climate://devices" in resources

        templates = {
            t.uri_template
            for t in (await session.list_resource_templates()).resource_templates
        }
        assert "climate://devices/{device_id}/reading" in templates
        assert "climate://devices/{device_id}/fan" in templates


@pytest.mark.asyncio
async def test_set_fan_state_forwards_to_transport(live_server):
    async with open_session(live_server) as session:
        result = await session.call_tool(
            "set_fan_state",
            {"command": "on", "device_id": "esp32-climate-01", "reason": "mcp test"},
        )
        assert not result.is_error
        body = _structured(result)
        assert body["command"] == "ON"
        assert body["source"] == "mcp"

    # Verify the command really landed in the transport layer over plain HTTP.
    async with httpx.AsyncClient() as http:
        polled = await http.get(
            f"{live_server}/api/fan/command", params={"device_id": "esp32-climate-01"}
        )
    assert polled.json()["command"] == "ON"
    assert polled.json()["source"] == "mcp"


@pytest.mark.asyncio
async def test_set_fan_state_rejects_invalid_command(live_server):
    async with open_session(live_server) as session:
        result = await session.call_tool("set_fan_state", {"command": "MAYBE"})
    assert result.is_error


@pytest.mark.asyncio
async def test_status_and_reading_tools_reflect_ingested_data(live_server):
    async with httpx.AsyncClient() as http:
        await http.post(
            f"{live_server}/api/climate",
            json={"device_id": "esp32-climate-01", "dht11_temperature": 31.5, "dht11_humidity": 55.0},
        )

    async with open_session(live_server) as session:
        status = _structured(await session.call_tool("get_system_status", {}))
        assert "esp32-climate-01" in status["devices"]
        assert status["devices"]["esp32-climate-01"]["reading"]["dht11_temperature"] == 31.5

        reading = _structured(
            await session.call_tool("get_latest_reading", {"device_id": "esp32-climate-01"})
        )
        assert reading["reading"]["dht11_humidity"] == 55.0


@pytest.mark.asyncio
async def test_resources_return_expected_payloads(live_server):
    async with httpx.AsyncClient() as http:
        await http.post(
            f"{live_server}/api/climate",
            json={"device_id": "esp32-climate-01", "dht11_temperature": 31.5, "dht11_humidity": 55.0},
        )

    async with open_session(live_server) as session:
        status = json.loads((await session.read_resource("climate://status")).contents[0].text)
        assert "esp32-climate-01" in status["devices"]

        devices = json.loads((await session.read_resource("climate://devices")).contents[0].text)
        assert devices == ["esp32-climate-01"]

        fan = json.loads(
            (await session.read_resource("climate://devices/esp32-climate-01/fan")).contents[0].text
        )
        assert fan["command"] == "ON"  # 31.5C crosses the rule-engine ON threshold


@pytest.mark.asyncio
async def test_recent_events_tool_observes_reading(live_server):
    """Subscribe first, then ingest, so the live reading event is captured."""

    async with open_session(live_server) as session:
        task = asyncio.create_task(
            session.call_tool("get_recent_events", {"limit": 5, "timeout": 3.0})
        )
        # Give the SSE consumer a moment to attach before producing an event.
        await asyncio.sleep(0.4)
        async with httpx.AsyncClient() as http:
            await http.post(
                f"{live_server}/api/climate",
                json={
                    "device_id": "esp32-climate-01",
                    "dht11_temperature": 29.0,
                    "dht11_humidity": 50.0,
                },
            )
        result = _structured(await task)

    assert result["count"] >= 1
    assert any(ev["event"] == "reading" for ev in result["events"])


@pytest.mark.asyncio
async def test_poll_and_reset_tools(live_server):
    async with open_session(live_server) as session:
        polled = _structured(
            await session.call_tool("poll_fan_command", {"device_id": "esp32-climate-01"})
        )
        assert polled["command"] in ("ON", "OFF")

        reset = _structured(
            await session.call_tool("reset_fan_auto", {"device_id": "esp32-climate-01"})
        )
        assert reset["status"] == "ok"


@pytest.mark.asyncio
async def test_auth_token_is_forwarded(token_server):
    """With CLIMATE_AUTH_TOKEN set, the MCP client must forward it or get a 401."""

    async with open_session(token_server, token="s3cret") as session:
        ok = await session.call_tool("get_system_status", {})
        assert not ok.is_error

    async with open_session(token_server) as session:
        denied = await session.call_tool("set_fan_state", {"command": "ON"})
        assert denied.is_error
