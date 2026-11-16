"""core_logic.py - the single source of truth for talking to the transport layer.

Every HTTP call to the FastAPI backend (``server/app.py``) lives here, so the two
consumers stay in sync and never re-implement the wire contract:

* ``mcp_server.py``      -- wraps these functions as MCP Tools / Resources.
* ``climate_agent.py``   -- drives its local autonomous loop with them.

The module is intentionally framework-free (plain ``httpx`` + stdlib). It exposes:

* ``TransportClient``  -- a persistent client with typed methods and unified error
  handling (``TransportError`` on connection / timeout / HTTP / decode failures).
* ``core_*``           -- thin module-level convenience functions that build a
  short-lived client per call, for scripts and one-shot use.

Error handling
--------------
Every method raises :class:`TransportError` when the backend is unreachable
(connection refused, connect/read timeout) or returns a non-2xx status, so callers
never have to know about ``httpx`` exceptions. The ``mcp`` layer surfaces these as
tool errors; the agent catches them to reconnect with backoff.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator, Optional

import httpx

# Defaults mirror the transport layer's own configuration (see server/config.py).
DEFAULT_BASE = os.getenv("CLIMATE_BASE", "http://127.0.0.1:8801")
DEFAULT_DEVICE = "esp32-climate-01"

# Per-request timeout: a connect timeout so "backend not running" fails fast, and
# a read timeout for normal (non-streaming) calls.
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 5.0


class TransportError(RuntimeError):
    """Raised for any failure while talking to the transport layer."""


class TransportTimeout(TransportError):
    """Raised when a streaming read times out (a recoverable, retryable condition)."""


def normalise_command(command: str) -> str:
    """Validate and normalise an ON/OFF command coming from an LLM or CLI."""

    value = str(command).strip().upper()
    if value not in ("ON", "OFF"):
        raise TransportError(f"command must be 'ON' or 'OFF', got {command!r}")
    return value


class TransportClient:
    """Persistent HTTP client for the FastAPI transport layer.

    Use as a context manager so the underlying connection pool is closed::

        with TransportClient(base_url=...) as client:
            status = client.get_system_status()
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        headers = {"X-Device-Token": token} if token else {}
        self._client = httpx.Client(headers=headers, timeout=self._timeout)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TransportClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level request helper -----------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue a request and translate any failure into ``TransportError``."""

        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.ConnectError as exc:
            raise TransportError(
                f"transport layer unreachable at {self.base_url} "
                f"(is `python main.py` running?)"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TransportError(f"transport layer timed out at {url}: {exc}") from exc
        except httpx.HTTPError as exc:  # any other transport-level failure
            raise TransportError(f"transport layer request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise TransportError(
                f"transport layer returned {response.status_code} for "
                f"{method} {path}: {detail}"
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise TransportError(f"invalid JSON from {url}: {exc}") from exc

    # -- typed API methods -------------------------------------------------
    def get_system_status(self) -> dict[str, Any]:
        """Full snapshot: settings, subscribers, devices, readings, fan state."""

        return self._request("GET", "/api/status")

    def get_latest_reading(self, device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Latest reading envelope for one device (raises if the device is unknown)."""

        device = self.get_system_status().get("devices", {}).get(device_id)
        if device is None:
            raise TransportError(f"unknown device_id: {device_id}")
        return device

    def control_fan(
        self,
        command: str,
        device_id: Optional[str] = None,
        reason: str = "",
        source: str = "core",
    ) -> dict[str, Any]:
        """Push an ON/OFF fan command (broadcast to all devices when no id given)."""

        body: dict[str, Any] = {
            "command": normalise_command(command),
            "reason": reason,
            "source": source,
        }
        if device_id is not None:
            body["device_id"] = device_id
        return self._request("POST", "/api/fan/command", json=body)

    def reset_fan_auto(self, device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Hand a device back to the deterministic rule engine."""

        return self._request("POST", "/api/fan/auto", params={"device_id": device_id})

    def poll_fan_command(
        self, device_id: str = DEFAULT_DEVICE, since_seq: int = 0, wait: bool = False
    ) -> dict[str, Any]:
        """Read (optionally long-poll) the queued fan command for a device."""

        params = {"device_id": device_id, "since_seq": since_seq, "wait": 1 if wait else 0}
        return self._request("GET", "/api/fan/command", params=params)

    # -- SSE stream --------------------------------------------------------
    def iter_events(
        self, read_timeout: float = DEFAULT_TIMEOUT, deadline: Optional[float] = None
    ) -> Iterator[tuple[str, Any]]:
        """Yield ``(event_name, data)`` tuples from ``/api/stream``.

        Runs until the stream ends or the connection drops; keep-alive comments are
        skipped. Raises :class:`TransportError` on connect failure so the caller can
        reconnect.

        ``deadline`` is an absolute ``time.monotonic()`` bound. Because the server
        sends periodic keep-alives, a read timeout alone would never fire while the
        stream is healthy; the deadline gives a hard upper bound on how long to
        wait. Hitting it raises :class:`TransportTimeout` (a recoverable condition).
        """

        timeout = httpx.Timeout(read_timeout, connect=DEFAULT_CONNECT_TIMEOUT)
        try:
            with self._client.stream(
                "GET", f"{self.base_url}/api/stream", timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    raise TransportError(
                        f"transport layer returned {response.status_code} for /api/stream"
                    )
                event_name = "message"
                for line in response.iter_lines():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TransportTimeout("event stream deadline reached")
                    if line == "":
                        event_name = "message"
                        continue
                    if line.startswith(":"):  # keep-alive comment
                        continue
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                        continue
                    if line.startswith("data:"):
                        yield event_name, json.loads(line.split(":", 1)[1].strip())
        except httpx.ConnectError as exc:
            raise TransportError(
                f"transport layer unreachable at {self.base_url} "
                f"(is `python main.py` running?)"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TransportTimeout(f"transport layer stream timed out: {exc}") from exc

    def recent_events(self, limit: int = 10, timeout: float = 2.0) -> list[dict[str, Any]]:
        """Collect up to ``limit`` non-snapshot events, bounded by ``timeout`` seconds.

        Never raises on timeout -- it simply returns whatever it gathered, so an MCP
        tool call always answers promptly. A genuinely unreachable backend still
        raises :class:`TransportError`.
        """

        events: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        try:
            for name, data in self.iter_events(read_timeout=timeout, deadline=deadline):
                if name != "snapshot":
                    events.append({"event": name, "data": data})
                    if len(events) >= limit:
                        break
        except TransportTimeout:
            pass  # time budget exhausted -> return what we have
        return events


# ---------------------------------------------------------------------------
# Module-level convenience API (one-shot client per call).
# ---------------------------------------------------------------------------
def _oneshot(base_url: str, token: Optional[str]) -> TransportClient:
    return TransportClient(base_url=base_url, token=token)


def core_get_system_status(base_url: str = DEFAULT_BASE, token: Optional[str] = None) -> dict[str, Any]:
    """Low-level: fetch the full system status snapshot."""

    with _oneshot(base_url, token) as client:
        return client.get_system_status()


def core_get_latest_reading(
    device_id: str = DEFAULT_DEVICE, base_url: str = DEFAULT_BASE, token: Optional[str] = None
) -> dict[str, Any]:
    """Low-level: fetch the latest reading for one device."""

    with _oneshot(base_url, token) as client:
        return client.get_latest_reading(device_id)


def core_control_fan(
    command: str,
    device_id: Optional[str] = None,
    reason: str = "",
    source: str = "core",
    base_url: str = DEFAULT_BASE,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Low-level: push an ON/OFF fan command to the transport layer."""

    with _oneshot(base_url, token) as client:
        return client.control_fan(command, device_id=device_id, reason=reason, source=source)


def core_reset_fan_auto(
    device_id: str = DEFAULT_DEVICE, base_url: str = DEFAULT_BASE, token: Optional[str] = None
) -> dict[str, Any]:
    """Low-level: return a device to rule-engine control."""

    with _oneshot(base_url, token) as client:
        return client.reset_fan_auto(device_id)


def core_poll_fan_command(
    device_id: str = DEFAULT_DEVICE,
    since_seq: int = 0,
    wait: bool = False,
    base_url: str = DEFAULT_BASE,
    token: Optional[str] = None,
) -> dict[str, Any]:
    """Low-level: read (optionally long-poll) the queued fan command."""

    with _oneshot(base_url, token) as client:
        return client.poll_fan_command(device_id, since_seq=since_seq, wait=wait)


def core_recent_events(
    limit: int = 10,
    timeout: float = 2.0,
    base_url: str = DEFAULT_BASE,
    token: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Low-level: collect a few recent events from the SSE stream."""

    with _oneshot(base_url, token) as client:
        return client.recent_events(limit=limit, timeout=timeout)
