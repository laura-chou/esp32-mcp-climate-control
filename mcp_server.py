"""mcp_server.py - MCP bridge over the FastAPI transport layer.

Exposes the transport layer (``server/app.py``) to any MCP client / LLM as
standard **Tools** and **Resources**. All backend calls go through
``core_logic.py`` (the single source of truth), so this module only handles the
MCP / JSON-RPC layer and adds no duplicate HTTP logic.

Tools
-----
get_system_status        Full snapshot (devices, readings, fan state).
get_latest_reading       Latest DHT11/PIR/MQ-135 reading for one device.
set_fan_state            Push an ON/OFF fan command (validated).
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
import json
import logging
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from core_logic import DEFAULT_BASE, DEFAULT_DEVICE, TransportClient

logger = logging.getLogger("climate.mcp")


def build_server(
    base_url: str = DEFAULT_BASE,
    token: Optional[str] = None,
    client: Optional[TransportClient] = None,
    name: str = "esp32-climate-control",
) -> MCPServer:
    """Build the MCP server exposing the climate transport layer.

    ``client`` may be injected for tests; otherwise a persistent ``TransportClient``
    is created from ``base_url`` / ``token``.
    """

    transport = client or TransportClient(base_url=base_url, token=token)
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
    def get_system_status() -> dict[str, Any]:
        """Return the full system snapshot: devices, latest readings and fan state."""

        return transport.get_system_status()

    @server.tool()
    def get_latest_reading(device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Return the latest DHT11/PIR/MQ-135 reading for one device."""

        return transport.get_latest_reading(device_id)

    @server.tool()
    def set_fan_state(
        command: str,
        device_id: Optional[str] = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Set the fan to ON or OFF.

        ``command`` must be "ON" or "OFF". ``device_id`` defaults to every known
        device when omitted. ``reason`` is a short human-readable justification.
        """

        return transport.control_fan(command, device_id=device_id, reason=reason, source="mcp")

    @server.tool()
    def reset_fan_auto(device_id: str = DEFAULT_DEVICE) -> dict[str, Any]:
        """Hand a device back to the deterministic rule engine (release MCP control)."""

        return transport.reset_fan_auto(device_id)

    @server.tool()
    def poll_fan_command(
        device_id: str = DEFAULT_DEVICE,
        since_seq: int = 0,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Read the queued fan command for a device (set ``wait`` to long-poll)."""

        return transport.poll_fan_command(device_id, since_seq=since_seq, wait=wait)

    @server.tool()
    def get_recent_events(limit: int = 10, timeout: float = 2.0) -> dict[str, Any]:
        """Return up to ``limit`` recent events from the /api/stream SSE stream."""

        events = transport.recent_events(limit=limit, timeout=timeout)
        return {"events": events, "count": len(events)}

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    @server.resource("climate://status", mime_type="application/json")
    def status_resource() -> str:
        """Full system snapshot as JSON."""

        return json.dumps(transport.get_system_status(), indent=2)

    @server.resource("climate://devices", mime_type="application/json")
    def devices_resource() -> str:
        """Known device ids as JSON."""

        return json.dumps(sorted(transport.get_system_status().get("devices", {})), indent=2)

    @server.resource("climate://devices/{device_id}/reading", mime_type="application/json")
    def reading_resource(device_id: str) -> str:
        """Latest reading for a device as JSON."""

        return json.dumps(transport.get_latest_reading(device_id), indent=2)

    @server.resource("climate://devices/{device_id}/fan", mime_type="application/json")
    def fan_resource(device_id: str) -> str:
        """Current fan command for a device as JSON."""

        device = transport.get_latest_reading(device_id)
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
