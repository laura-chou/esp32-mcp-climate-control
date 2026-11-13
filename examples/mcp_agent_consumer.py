"""Reference MCP core / climate_agent consumer.

This shows the two halves of the MCP side:

1. Subscribe to ``/api/stream`` (SSE) to receive ``reading`` events.
2. Decide on a fan state (here: a placeholder standing in for the Ollama /
   Qwen2.5 reasoning step) and push it back via ``POST /api/fan/command``.

Run the server first, then:

    python examples/mcp_agent_consumer.py
"""

from __future__ import annotations

import argparse
import json

import httpx

DEFAULT_BASE = "http://127.0.0.1:8801"


def decide_with_placeholder_agent(reading: dict) -> tuple[str, str]:
    """Stand-in for the Ollama reasoning loop.

    Replace this body with a call to ``climate_agent.py`` / Ollama and return
    the model's ON/OFF decision plus a short rationale.
    """

    temperature = reading["dht11_temperature"]
    humidity = reading["dht11_humidity"]
    motion = bool(reading.get("pir_motion", False))
    mq = bool(reading.get("mq_triggered", False))
    # Air quality overrides everything; then comfort; then occupancy/energy.
    if mq:
        return "ON", "agent: MQ-135 triggered -> ventilate"
    if temperature >= 29.0 or humidity >= 72.0:
        return "ON", f"agent: T={temperature:.1f}C H={humidity:.1f}% -> cool/dry"
    if not motion:
        return "OFF", "agent: comfortable and unoccupied -> energy saving"
    if temperature <= 25.5 and humidity <= 60.0:
        return "OFF", f"agent: T={temperature:.1f}C H={humidity:.1f}% -> comfortable"
    return "ON", "agent: holding previous decision"


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP core / climate_agent consumer")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    headers = {"X-Device-Token": args.token} if args.token else {}

    with httpx.Client(headers=headers, timeout=None) as client:
        with client.stream("GET", f"{args.base}/api/stream") as stream:
            event_name = "message"
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event_name = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                data = json.loads(line.split(":", 1)[1].strip())
                if event_name == "reading":
                    print(f"[READ] {data['device_id']} T={data['dht11_temperature']} "
                          f"H={data['dht11_humidity']} motion={data.get('pir_motion')} "
                          f"mq={data.get('mq_triggered')}")
                    command, reason = decide_with_placeholder_agent(data)
                    resp = client.post(
                        f"{args.base}/api/fan/command",
                        json={
                            "command": command,
                            "reason": reason,
                            "source": "climate_agent",
                            "device_id": data["device_id"],
                        },
                    )
                    resp.raise_for_status()
                    print(f"[CMD ] pushed fan={command} ({reason})")
                elif event_name == "snapshot":
                    print(f"[INIT] snapshot seq={data.get('seq')}")


if __name__ == "__main__":
    main()
