"""Tests for core_logic.py - the shared transport-layer client (real code, no mocks).

Covers both the happy path (against a real uvicorn instance of the FastAPI app) and
the error handling that the refactor introduced: unreachable backend, HTTP errors
and invalid commands must all surface as ``TransportError``.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

import core_logic
from core_logic import TransportClient, TransportError, normalise_command
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


def _start_server(settings: Settings) -> tuple[str, uvicorn.Server]:
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
    return f"http://127.0.0.1:{port}", server


@pytest.fixture
def live_server():
    base, server = _start_server(make_settings())
    try:
        yield base
    finally:
        server.should_exit = True


# ---------------------------------------------------------------------------
# normalise_command: pure validation
# ---------------------------------------------------------------------------


def test_normalise_command_accepts_case_and_whitespace():
    assert normalise_command("on") == "ON"
    assert normalise_command(" Off ") == "OFF"


def test_normalise_command_rejects_garbage():
    with pytest.raises(TransportError):
        normalise_command("MAYBE")


# ---------------------------------------------------------------------------
# Happy path against a live backend
# ---------------------------------------------------------------------------


def test_status_and_control_fan_round_trip(live_server):
    with TransportClient(base_url=live_server) as client:
        status = client.get_system_status()
        assert "devices" in status

        result = client.control_fan("on", device_id="esp32-climate-01", reason="test", source="core")
        assert result["command"] == "ON"
        assert result["source"] == "core"

        polled = client.poll_fan_command("esp32-climate-01")
        assert polled["command"] == "ON"


def test_unknown_device_raises_transport_error(live_server):
    with TransportClient(base_url=live_server) as client:
        with pytest.raises(TransportError, match="unknown device_id"):
            client.get_latest_reading("does-not-exist")


def test_module_level_convenience_functions(live_server):
    httpx.post(
        f"{live_server}/api/climate",
        json={"device_id": "esp32-climate-01", "dht11_temperature": 24.0, "dht11_humidity": 50.0},
    )
    core_logic.core_control_fan("OFF", device_id="esp32-climate-01", base_url=live_server)
    status = core_logic.core_get_system_status(base_url=live_server)
    assert status["devices"]["esp32-climate-01"]["fan_command"]["command"] == "OFF"


# ---------------------------------------------------------------------------
# Error handling: backend not running / wrong token
# ---------------------------------------------------------------------------


def test_unreachable_backend_raises_transport_error():
    """A closed port must fail fast with a helpful TransportError, not a raw httpx error."""

    port = _free_port()  # bound then released -> nothing is listening
    client = TransportClient(base_url=f"http://127.0.0.1:{port}", connect_timeout=1.0)
    try:
        with pytest.raises(TransportError, match="unreachable"):
            client.get_system_status()
    finally:
        client.close()


def test_auth_failure_raises_transport_error():
    base, server = _start_server(make_settings(auth_token="s3cret"))
    try:
        with TransportClient(base_url=base) as client:  # no token
            with pytest.raises(TransportError, match="401"):
                client.control_fan("ON")
    finally:
        server.should_exit = True


def test_bad_command_is_rejected_before_request(live_server):
    with TransportClient(base_url=live_server) as client:
        with pytest.raises(TransportError, match="ON.*OFF"):
            client.control_fan("sideways")


def test_recent_events_returns_promptly_when_idle(live_server):
    """A quiet stream must not hang: the deadline returns an empty list, not a block."""

    with TransportClient(base_url=live_server) as client:
        started = time.monotonic()
        events = client.recent_events(limit=5, timeout=0.5)
        elapsed = time.monotonic() - started
    assert events == []
    assert elapsed < 3.0  # bounded by the deadline, not the (endless) stream
