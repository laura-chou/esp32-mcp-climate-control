"""climate_agent.py - the environment agent (AI brain).

This is the ``climate_agent.py`` box in the architecture diagram. It closes the
control loop:

    FastAPI /api/stream --(reading event)--> climate_agent
    climate_agent --(prompt)--> Ollama (Qwen2.5 / Llama3) --(decision)-->
    climate_agent --(POST /api/fan/command)--> FastAPI --> ESP32

It subscribes to the transport layer's SSE stream, asks a local Ollama model for
a fan decision on every DHT11 reading, and pushes the command back. When Ollama
is unavailable or returns something unusable it falls back to the deterministic
rule engine in ``server/rules.py`` so the loop never stalls.

Run the server first, then:

    python climate_agent.py --model qwen2.5

Preview decisions without touching the fan:

    python climate_agent.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass

import httpx

from server.config import Settings, settings as default_settings
from server.models import FanState
from server.rules import decide_fan

DEFAULT_BASE = "http://127.0.0.1:8801"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"

SYSTEM_PROMPT = (
    "You are climate_agent, the environment control agent for a room. "
    "You receive DHT11 temperature (Celsius) and relative humidity (percent) "
    "readings and decide whether the cooling fan should be ON or OFF. "
    "Keep the room comfortable (roughly 24-27 C, 40-60% humidity). "
    "Avoid rapid toggling: only change the current fan state when the reading "
    "clearly justifies it, otherwise keep the current state. "
    'Reply with ONLY a JSON object of the form '
    '{"command": "ON" or "OFF", "reason": "<short reason>"}.'
)


@dataclass
class Decision:
    """A fan decision produced by the agent (or its fallback)."""

    command: str  # "ON" | "OFF"
    reason: str
    source: str = "climate_agent"


def parse_decision(text: str) -> tuple[str, str] | None:
    """Extract ``(command, reason)`` from a model reply.

    Tolerates code fences and surrounding prose; returns ``None`` when no usable
    ON/OFF decision can be found.
    """

    if not text:
        return None

    candidate = text.strip()
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None

    try:
        payload = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    command = str(payload.get("command", "")).strip().upper()
    if command not in ("ON", "OFF"):
        return None

    reason = str(payload.get("reason", "")).strip()
    return command, reason


class OllamaClient:
    """Thin synchronous client for the Ollama chat API."""

    def __init__(self, host: str = DEFAULT_OLLAMA, timeout: float = 60.0) -> None:
        self._client = httpx.Client(base_url=host, timeout=timeout)

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        response = self._client.post(
            "/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2},
            },
        )
        response.raise_for_status()
        return response.json()["message"]["content"]

    def close(self) -> None:
        self._client.close()


class Agent:
    """Turns readings into fan decisions using a chat client plus a fallback."""

    def __init__(
        self,
        chat_client,
        model: str,
        min_switch_seconds: float = 15.0,
        settings: Settings = default_settings,
    ) -> None:
        self._chat = chat_client
        self._model = model
        self.min_switch_seconds = min_switch_seconds
        self._settings = settings
        self._states: dict[str, str] = {}
        self._last_switch: dict[str, float] = {}

    def remember(self, device_id: str, command: str) -> None:
        """Seed the known fan state for a device (e.g. from /api/status)."""

        self._states[device_id] = command.upper()

    def current_state(self, device_id: str) -> str:
        return self._states.get(device_id, "OFF")

    def build_messages(self, reading: dict, current: str) -> list[dict[str, str]]:
        s = self._settings
        user = (
            f"device_id: {reading['device_id']}\n"
            f"dht22_temperature: {reading['dht22_temperature']}\n"
            f"dht22_humidity: {reading['dht22_humidity']}\n"
            f"current_fan: {current}\n"
            f"comfort_band_c: {s.temp_off_c}-{s.temp_on_c}\n"
            f"humidity_band_pct: {s.humidity_off}-{s.humidity_on}\n"
            "Decide the fan command now."
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _is_urgent(self, reading: dict) -> bool:
        """True when the reading is far enough outside the band to override
        the anti-flapping guard."""

        s = self._settings
        temperature = reading["dht22_temperature"]
        humidity = reading["dht22_humidity"]
        return (
            temperature >= s.temp_on_c + 3.0
            or humidity >= s.humidity_on + 5.0
            or temperature <= s.temp_off_c - 3.0
        )

    def _rule_fallback(self, reading: dict, current: str) -> Decision:
        state, reason = decide_fan(
            reading["dht22_temperature"],
            reading["dht22_humidity"],
            FanState(current),
            self._settings,
        )
        return Decision(state.value, reason, "rule-fallback")

    def decide(self, reading: dict) -> Decision:
        device_id = reading["device_id"]
        current = self.current_state(device_id)

        try:
            raw = self._chat.chat(self._model, self.build_messages(reading, current))
            parsed = parse_decision(raw)
        except Exception as exc:  # network error, bad JSON, missing keys, ...
            print(f"[WARN] ollama call failed: {exc}", file=sys.stderr)
            parsed = None

        if parsed is None:
            decision = self._rule_fallback(reading, current)
        else:
            command, reason = parsed
            decision = Decision(command, reason, "climate_agent")

        decision = self._apply_guard(reading, current, decision)
        self._states[device_id] = decision.command
        return decision

    def _apply_guard(self, reading: dict, current: str, decision: Decision) -> Decision:
        """Suppress rapid flips from model nondeterminism, unless urgent."""

        if (
            self.min_switch_seconds > 0
            and decision.command != current
            and not self._is_urgent(reading)
        ):
            last = self._last_switch.get(reading["device_id"])
            if last is not None and (time.monotonic() - last) < self.min_switch_seconds:
                held = int(time.monotonic() - last)
                return Decision(
                    current,
                    f"holding {current}: min switch interval not elapsed ({held}s)",
                    decision.source,
                )

        if decision.command != current:
            self._last_switch[reading["device_id"]] = time.monotonic()
        return decision


def _seed_states(client: httpx.Client, base: str, agent: Agent) -> None:
    """Load current fan commands so the first decision knows the baseline."""

    try:
        status = client.get(f"{base}/api/status").json()
    except Exception as exc:
        print(f"[WARN] could not read /api/status: {exc}", file=sys.stderr)
        return
    for device_id, info in (status.get("devices") or {}).items():
        command = (info.get("fan_command") or {}).get("command")
        if command:
            agent.remember(device_id, command)


def _push_command(
    client: httpx.Client, base: str, reading: dict, decision: Decision
) -> None:
    response = client.post(
        f"{base}/api/fan/command",
        json={
            "command": decision.command,
            "reason": decision.reason,
            "source": decision.source,
            "device_id": reading["device_id"],
        },
    )
    response.raise_for_status()


def consume_stream(args: argparse.Namespace, agent: Agent) -> None:
    """Subscribe to /api/stream and handle reading events until the stream ends."""

    headers = {"X-Device-Token": args.token} if args.token else {}
    with httpx.Client(headers=headers, timeout=None) as client:
        _seed_states(client, args.base, agent)
        with client.stream("GET", f"{args.base}/api/stream") as stream:
            event = "message"
            for line in stream.iter_lines():
                if line == "":
                    event = "message"
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue

                data = json.loads(line.split(":", 1)[1].strip())
                if event == "reading":
                    _handle_reading(client, args, agent, data)
                elif event == "snapshot":
                    print(f"[INIT] snapshot seq={data.get('seq')}", flush=True)


def _handle_reading(
    client: httpx.Client, args: argparse.Namespace, agent: Agent, reading: dict
) -> None:
    decision = agent.decide(reading)
    print(
        f"[DECIDE] {reading['device_id']} T={reading['dht22_temperature']} "
        f"H={reading['dht22_humidity']} -> {decision.command} "
        f"({decision.source}: {decision.reason})",
        flush=True,
    )
    if args.dry_run:
        return
    try:
        _push_command(client, args.base, reading, decision)
    except Exception as exc:
        print(f"[WARN] failed to push command: {exc}", file=sys.stderr)


def run(args: argparse.Namespace) -> None:
    chat_client = OllamaClient(args.ollama)
    agent = Agent(
        chat_client,
        model=args.model,
        min_switch_seconds=args.min_switch_seconds,
    )
    backoff = 1
    try:
        while True:
            try:
                consume_stream(args, agent)
                backoff = 1
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[WARN] stream error: {exc}; reconnecting in {backoff}s",
                    file=sys.stderr,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
    except KeyboardInterrupt:
        print("\n[INFO] climate_agent stopped", file=sys.stderr)
    finally:
        chat_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="climate_agent: Ollama-driven fan controller")
    parser.add_argument("--base", default=DEFAULT_BASE, help="transport layer base URL")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA, help="Ollama base URL")
    parser.add_argument("--model", default="qwen2.5", help="Ollama model name")
    parser.add_argument("--token", default=None, help="shared token (X-Device-Token)")
    parser.add_argument(
        "--min-switch-seconds",
        type=float,
        default=15.0,
        help="minimum seconds between non-urgent fan flips (0 disables)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print decisions without pushing commands",
    )
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
