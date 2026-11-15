"""MCP server: wraps the FastAPI transport layer as Model Context Protocol tools.

This module exposes the existing HTTP / SSE transport (``server/app.py``) to any
MCP-capable client or LLM as standard **Tools** and **Resources**, without
changing the FastAPI routes or their ``CLIMATE_AUTH_TOKEN`` guard. The MCP server
is a thin client of the HTTP layer: every call is translated into the same
request the ESP32 / ``climate_agent.py`` already make.

Tools
-----
get_system_status        Full snapshot (devices, readings, fan state).
get_latest_reading       Latest DHT11/PIR/MQ-135 reading for one device.
set_fan_state            Push an ON/OFF fan command (safe, validated).
reset_fan_auto           Hand a device back to the rule engine.
poll_fan_command         Read (optionally long-poll) the queued fan command.
get_recent_events        Drain a few events from the /api/stream SSE stream.

Resources
---------
climate://status                          Full status snapshot (JSON).
climate://devices                         Known device ids (JSON).
climate://devices/{device_id}/reading     Latest reading for a device.
climate://devices/{device_id}/fan         Current fan command for a device.

Run it (stdio transport, for Claude Desktop / MCP Inspector):

    python mcp_server.py --base http://127.0.0.1:8801

or over Streamable HTTP:

    python mcp_server.py --transport streamable-http --host 127.0.0.1 --port 8901
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx

from mcp.server.mcpserver import MCPServer

logger = logging.getLogger("climate.mcp")

DEFAULT_BASE = "http://127.0.0.1:8801"
DEFAULT_DEVICE = "esp32-climate-01"


class ClimateTransportClient:
    """Async HTTP client for the FastAPI transport layer.

    The token (when set) is sent as ``X-Device-Token`` on every request, matching
    the ``require_token`` dependency in ``server/app.py``.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE,
        token: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"X-Device-Token": token} if token else {}
        self._client = client or httpx.AsyncClient(headers=headers, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, f"{self.base_url}{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/api/status")

    async def latest_reading(self, device_id: str) -> dict[str, Any]:
        snapshot = await self.status()
        device = snapshot.get("devices", {}).get(device_id)
        if device is None:
            raise ValueError(f"unknown device_id: {device_id}")
        return device

    async def push_fan_command(
        self,
        command: str,
        device_id: Optional[str] = None,
        reason: str = "",
        source: str = "mcp",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"command": command, "reason": reason, "source": source}
        if device_id is not None:
            body["device_id"] = device_id
        return await self._request("POST", "/api/fan/command", json=body)

    async def reset_auto(self, device_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/api/fan/auto", params={"device_id": device_id}
        )

    async def poll_fan_command(
        self, device_id: str, since_seq: int = 0, wait: bool = False
    ) -> dict[str, Any]:
        params = {
            "device_id": device_id,
            "since_seq": since_seq,
            "wait": 1 if wait else 0,
        }
        return await self._request("GET", "/api/fan/command", params=params)

    async def recent_events(
        self, limit: int = 10, timeout: float = 2.0
    ) -> list[dict[str, Any]]:
        """Collect up to ``limit`` non-snapshot events from the SSE stream.

        Bounded by ``timeout`` seconds so the tool always returns promptly.
        """

        events: list[dict[str, Any]] = []
        event_name = "message"

        async def _drain() -> None:
            nonlocal event_name
            async with self._client.stream("GET", f"{self.base_url}/api/stream") as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line.split(":", 1)[1].strip())
                        if event_name != "snapshot":
                            events.append({"event": event_name, "data": data})
                            if len(events) >= limit:
                                return

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("recent_events timed out after %.1fs with %d event(s)", timeout, len(events))
        return events


def _fan_state(command: str) -> str:
    """Validate and normalise an ON/OFF command from an LLM."""

    value = str(command).strip().upper()
    if value not in ("ON", "OFF"):
        raise ValueError(f"command must be 'ON' or 'OFF', got {command!r}")
    return value


def build_server(
    base_url: str = DEFAULT_BASE,
    token: Optional[str] = None,
    client: Optional[ClimateTransportClient] = None,
    name: str = "esp32-climate-control",
) -> MCPServer:
    """Build the MCP server exposing the climate transport layer."""

    transport = client or ClimateTransportClient(base_url=base_url, token=token)
    server = MCPServer(
        name,
        title="ESP32 Climate Control",
        instructions=(
            "Tools and resources for the ESP32 / FastAPI / Ollama climate-control "
            "system. Read system status and sensor readings, observe live events, "
            "and safely issue ON/OFF fan commands."
        ),
    )

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    @server.tool()
    async def get_system_status() -> dict[str, Any]:
        """Return the full system snapshot: devices, latest readings and fan state."""

        return await transport.status()

    @server.tool()
    async def get_latest_reading(device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Return the latest DHT11/PIR/MQ-135 reading for one device."""

        return await transport.latest_reading(device_id)

    @server.tool()
    async def set_fan_state(
        command: str,
        device_id: Optional[str] = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Set the fan to ON or OFF.

        ``command`` must be "ON" or "OFF". ``device_id`` defaults to every known
        device when omitted. ``reason`` is a short human-readable justification.
        """

        return await transport.push_fan_command(
            command=_fan_state(command), device_id=device_id, reason=reason, source="mcp"
        )

    @server.tool()
    async def reset_fan_auto(device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Hand a device back to the deterministic rule engine (release MCP control)."""

        return await transport.reset_auto(device_id)

    @server.tool()
    async def poll_fan_command(
        device_id: str = DEFAULT_DEVICE,
        since_seq: int = 0,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Read the queued fan command for a device (set ``wait`` to long-poll)."""

        return await transport.poll_fan_command(device_id, since_seq=since_seq, wait=wait)

    @server.tool()
    async def get_recent_events(limit: int = 10, timeout: float = 2.0) -> dict[str, Any]:
        """Return up to ``limit`` recent events from the /api/stream SSE stream."""

        events = await transport.recent_events(limit=limit, timeout=timeout)
        return {"events": events, "count": len(events)}

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    @server.resource("climate://status", mime_type="application/json")
    async def status_resource() -> str:
        """Full system snapshot as JSON."""

        return json.dumps(await transport.status(), indent=2)

    @server.resource("climate://devices", mime_type="application/json")
    async def devices_resource() -> str:
        """Known device ids as JSON."""

        snapshot = await transport.status()
        return json.dumps(sorted(snapshot.get("devices", {})), indent=2)

    @server.resource("climate://devices/{device_id}/reading", mime_type="application/json")
    async def reading_resource(device_id: str) -> str:
        """Latest reading for a device as JSON."""

        return json.dumps(await transport.latest_reading(device_id), indent=2)

    @server.resource("climate://devices/{device_id}/fan", mime_type="application/json")
    async def fan_resource(device_id: str) -> str:
        """Current fan command for a device as JSON."""

        device = await transport.latest_reading(device_id)
        return json.dumps(device.get("fan_command"), indent=2)

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP server for the climate transport layer")
    parser.add_argument("--base", default=DEFAULT_BASE, help="FastAPI transport base URL")
    parser.add_argument("--token", default=None, help="Value for X-Device-Token")
    parser.add_argument(
        "--transport", default="stdio", choices=("stdio", "streamable-http", "sse")
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8901)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    server = build_server(base_url=args.base, token=args.token)

    if args.transport == "stdio":
        server.run("stdio")
    elif args.transport == "streamable-http":
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
