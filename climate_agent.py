"""climate_agent.py - the environment agent (AI brain).

This is the ``climate_agent.py`` box in the architecture diagram. It closes the
control loop:

    FastAPI /api/stream --(reading event)--> climate_agent
    climate_agent --(prompt)--> Ollama (Qwen2.5 / Llama3) --(decision)-->
    climate_agent --(POST /api/fan/command)--> FastAPI --> ESP32

It subscribes to the transport layer's SSE stream, asks a local Ollama model to
decide on every DHT11 reading, and pushes the resulting command back.

Hybrid decision model
---------------------
There is a single decision path (no mode switch). The work is split by what each
side is good at, and every layer below degrades gracefully into the next:

* **Air-quality floor** -- a triggered MQ-135 forces ventilation in code, before
  the model is even consulted.
* **Human-first yield** -- if a human has taken manual control (e.g. Claude
  Desktop over MCP), the loop skips its LLM reasoning and never writes a fan
  command, so it cannot clobber the manual setting until ``reset_fan_auto``.
* **Climate preprocessing** -- ``server/rules.py`` computes ``climate_needs_cooling``
  and ``climate_is_extreme`` with hysteresis, so the model never compares numbers
  itself (which is where small local models hallucinate and flap around a
  threshold). The model receives these *semantic* flags, not the raw figures.
* **LLM scheduling** -- the model weighs ``pir_motion`` (occupancy / energy) and
  ``current_fan`` to make the final call, wrapped in a code-enforced safety
  envelope.
* **Automatic fallback** -- if Ollama is offline, times out, or returns invalid
  JSON, the exception is caught and ``server/rules.py``'s ``decide_fan`` takes
  over, so the loop keeps running with no AI.

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

from core_logic import DEFAULT_BASE, TransportClient, TransportError
from server.config import Settings, settings as default_settings
from server.models import FanState
from server.rules import climate_is_extreme, climate_needs_cooling, decide_fan

DEFAULT_OLLAMA = "http://127.0.0.1:11434"

# Fan-command sources that mean a human took the wheel (e.g. Claude Desktop over
# MCP). While one of these owns the device the agent yields instead of deciding.
MANUAL_OVERRIDE_SOURCES = frozenset({"mcp", "manual"})

SYSTEM_PROMPT = (
    "You are climate_agent, the multimodal environment control agent for a room. "
    "The controller has ALREADY evaluated the temperature and humidity for you, "
    "so you must NOT compare the raw numbers yourself -- do not do arithmetic. "
    "You receive these fields: "
    "climate_needs_cooling (bool): the temperature/humidity verdict, computed with "
    "hysteresis by a deterministic rule engine; "
    "climate_extreme (bool): the climate is far outside the comfort band (safety); "
    "pir_motion (bool): whether someone is present; "
    "mq_triggered (bool): whether the MQ-135 air-quality threshold is exceeded; "
    "current_fan: the fan state right now. "
    "Decide the final fan command (ON or OFF) by weighing occupancy and energy: "
    "(1) if climate_extreme is true, keep the fan ON; "
    "(2) if climate_needs_cooling is true and pir_motion is true, keep the fan ON; "
    "(3) if climate_needs_cooling is true but pir_motion is false and the climate "
    "is not extreme, you MAY keep the fan OFF to save energy; "
    "(4) if climate_needs_cooling is false, prefer OFF. "
    "Avoid rapid toggling: prefer keeping current_fan unless the flags clearly "
    "justify a change. "
    "Do NOT restate the numbers in your reason -- the controller adds the exact "
    "figures itself, so any number you write may be wrong. Keep the reason short. "
    'Reply with ONLY a JSON object of the form '
    '{"command": "ON" or "OFF", "reason": "<short reason>"}.'
)


@dataclass
class Decision:
    """A fan decision produced by the agent (or its fallback)."""

    command: str  # "ON" | "OFF"
    reason: str
    source: str = "climate_agent"
    advice: str = ""  # the model's advisory text, when it was consulted


@dataclass
class Advice:
    """A command proposed by the model (validated before it is accepted)."""

    command: str  # "ON" | "OFF"
    reason: str


def parse_decision(text: str) -> tuple[str, str] | None:
    """Extract ``(command, reason)`` from a model reply.

    Tolerates code fences and surrounding prose; returns ``None`` when no usable
    ON/OFF decision can be found. An empty reason is allowed (the model may
    abstain from explaining).
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
    """Turns readings into fan decisions using the single hybrid path.

    The rule engine precomputes the temperature/humidity verdict
    (``climate_needs_cooling`` / ``climate_is_extreme``) and the model weighs
    occupancy (PIR) to decide the final command within a code-enforced safety
    envelope. When the model is unreachable or returns unusable output, the
    deterministic ``decide_fan`` rule engine takes over automatically.
    """

    def __init__(
        self,
        chat_client,
        model: str,
        min_switch_seconds: float = 30.0,
        settings: Settings = default_settings,
        dry_run: bool = False,
    ) -> None:
        self._chat = chat_client
        self._model = model
        self.min_switch_seconds = min_switch_seconds
        self._settings = settings
        self.dry_run = dry_run
        self._states: dict[str, str] = {}
        self._last_switch: dict[str, float] = {}

    def remember(self, device_id: str, command: str) -> None:
        """Seed the known fan state for a device (e.g. from /api/status)."""

        self._states[device_id] = command.upper()

    def current_state(self, device_id: str) -> str:
        return self._states.get(device_id, "OFF")

    def climate_needs_cooling(self, reading: dict, current: str) -> bool:
        """Deterministic temperature/humidity verdict (hysteresis, no LLM)."""

        return climate_needs_cooling(
            reading["dht11_temperature"],
            reading["dht11_humidity"],
            FanState(current),
            self._settings,
        )

    def build_messages(self, reading: dict, current: str) -> list[dict[str, str]]:
        s = self._settings
        needs_cooling = self.climate_needs_cooling(reading, current)
        extreme = climate_is_extreme(
            reading["dht11_temperature"], reading["dht11_humidity"], s
        )
        user = (
            f"device_id: {reading['device_id']}\n"
            f"climate_needs_cooling: {needs_cooling}\n"
            f"climate_extreme: {extreme}\n"
            f"pir_motion: {bool(reading.get('pir_motion', False))}\n"
            f"mq_triggered: {bool(reading.get('mq_triggered', False))}\n"
            f"current_fan: {current}\n"
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
        return bool(reading.get("mq_triggered", False)) or climate_is_extreme(
            reading["dht11_temperature"], reading["dht11_humidity"], s
        )

    def _describe(self, command: str, reading: dict, current: str) -> str:
        """Render a factually-correct reason for ``command`` from the numbers.

        The model's free text is never used here, so the reason can never
        contradict the reading -- including when the model overrides the band.
        """

        t = reading["dht11_temperature"]
        h = reading["dht11_humidity"]
        motion = bool(reading.get("pir_motion", False))
        mq = bool(reading.get("mq_triggered", False))
        needs_cooling = self.climate_needs_cooling(reading, current)
        extreme = climate_is_extreme(t, h, self._settings)

        if command == "ON":
            if mq:
                return (
                    f"ON: MQ-135 air quality threshold exceeded -> ventilate "
                    f"(T={t:.1f}C, H={h:.1f}%)"
                )
            if needs_cooling and extreme:
                return f"ON: climate extreme, must cool (T={t:.1f}C, H={h:.1f}%)"
            if needs_cooling:
                return f"ON: climate needs cooling (T={t:.1f}C, H={h:.1f}%)"
            if current == "ON":
                return f"holding ON (T={t:.1f}C, H={h:.1f}%)"
            return (
                f"ON (model override): within comfortable range "
                f"(T={t:.1f}C, H={h:.1f}%)"
            )

        # command == "OFF"
        if mq:
            return (
                f"OFF (model override): MQ-135 threshold exceeded "
                f"(T={t:.1f}C, H={h:.1f}%) -- ventilation recommended"
            )
        if needs_cooling and not extreme:
            return (
                f"OFF (energy saving): climate needs cooling but unoccupied "
                f"(T={t:.1f}C, H={h:.1f}%)"
            )
        if needs_cooling:
            return (
                f"OFF (model override): climate extreme, cooling recommended "
                f"(T={t:.1f}C, H={h:.1f}%)"
            )
        suffix = " and unoccupied" if not motion else ""
        return f"OFF: within comfortable range (T={t:.1f}C, H={h:.1f}%){suffix}"

    def _rule_decision(self, reading: dict, current: str) -> Decision:
        state, reason = decide_fan(
            reading["dht11_temperature"],
            reading["dht11_humidity"],
            FanState(current),
            self._settings,
            pir_motion=bool(reading.get("pir_motion", False)),
            mq_triggered=bool(reading.get("mq_triggered", False)),
        )
        return Decision(state.value, reason, "rule-engine")

    def _consult(self, reading: dict, current: str) -> Advice | None:
        """Ask the model for a command. Returns ``None`` when it is unusable
        (unreachable, timeout, or invalid JSON) so the caller can fall back."""

        try:
            raw = self._chat.chat(self._model, self.build_messages(reading, current))
            parsed = parse_decision(raw)
        except Exception as exc:  # network error, timeout, bad JSON, missing keys
            print(f"[WARN] ollama call failed: {exc} -> falling back to rule engine", file=sys.stderr)
            return None
        if parsed is None:
            print(
                "[WARN] ollama returned no usable JSON -> falling back to rule engine",
                file=sys.stderr,
            )
            return None
        command, reason = parsed
        return Advice(command, reason)

    def decide(self, reading: dict) -> Decision:
        device_id = reading["device_id"]
        current = self.current_state(device_id)

        # Air-quality floor: a triggered MQ-135 forces ventilation in code, before
        # the model is consulted, so bad air can never be overruled.
        if bool(reading.get("mq_triggered", False)):
            decision = self._rule_decision(reading, current)
        else:
            decision = self._decide_hybrid(reading, current)

        decision = self._apply_guard(reading, current, decision)
        self._states[device_id] = decision.command
        return decision

    def _decide_hybrid(self, reading: dict, current: str) -> Decision:
        """Rules precompute the climate flag; the model weighs PIR/occupancy.

        The model's command is only accepted inside a code-enforced safety
        envelope, so a small model can still make the final multimodal call
        without ever being able to (a) leave the fan off in an extreme climate or
        (b) turn the fan off to "save energy" when cooling is actually required
        *and* someone is present. When the model is unreachable or returns
        unusable output, the deterministic rule engine takes over (fallback).
        """

        advice = self._consult(reading, current)
        if advice is None:
            return self._rule_decision(reading, current)

        needs_cooling = self.climate_needs_cooling(reading, current)
        extreme = climate_is_extreme(
            reading["dht11_temperature"], reading["dht11_humidity"], self._settings
        )
        motion = bool(reading.get("pir_motion", False))

        command = advice.command
        if command == "OFF" and (extreme or (needs_cooling and motion)):
            command = "ON"
        return Decision(
            command,
            self._describe(command, reading, current),
            "climate_agent",
            advice.reason,
        )

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
                    decision.advice,
                )

        if decision.command != current:
            self._last_switch[reading["device_id"]] = time.monotonic()
        return decision


def _seed_states(client: TransportClient, agent: Agent) -> None:
    """Load current fan commands so the first decision knows the baseline."""

    try:
        status = client.get_system_status()
    except TransportError as exc:
        print(f"[WARN] could not read /api/status: {exc}", file=sys.stderr)
        return
    for device_id, info in (status.get("devices") or {}).items():
        command = (info.get("fan_command") or {}).get("command")
        if command:
            agent.remember(device_id, command)


def _push_command(client: TransportClient, reading: dict, decision: Decision) -> None:
    client.control_fan(
        decision.command,
        device_id=reading["device_id"],
        reason=decision.reason,
        source=decision.source,
    )


def manual_override_active(status: dict, device_id: str) -> bool:
    """True when ``/api/status`` reports a human (e.g. MCP) owning ``device_id``.

    The transport layer exposes this two ways; either is enough: the
    ``manual_override`` boolean, or a ``control_source`` naming a manual owner.
    """

    device = (status.get("devices") or {}).get(device_id) or {}
    if device.get("manual_override") is True:
        return True
    return device.get("control_source") in MANUAL_OVERRIDE_SOURCES


def _agent_must_yield(client: TransportClient, device_id: str) -> bool:
    """Poll the system status and report whether the agent should stand down.

    A status read failure is treated as "no override" (fail-open) so a transient
    transport blip cannot stall the control loop; the agent just keeps deciding.
    """

    try:
        status = client.get_system_status()
    except TransportError as exc:
        print(f"[WARN] could not read /api/status: {exc}", file=sys.stderr)
        return False
    return manual_override_active(status, device_id)


def consume_stream(client: TransportClient, agent: Agent) -> None:
    """Subscribe to /api/stream and handle reading events until the stream ends."""

    _seed_states(client, agent)
    for event, data in client.iter_events():
        if event == "reading":
            _handle_reading(client, agent, data)
        elif event == "snapshot":
            print(f"[INIT] snapshot seq={data.get('seq')}", flush=True)


def _handle_reading(client: TransportClient, agent: Agent, reading: dict) -> None:
    device_id = reading["device_id"]

    # Human-first: if Claude Desktop (or any MCP client) has taken manual control,
    # yield completely -- no LLM call, no set_fan_state -- until reset_fan_auto.
    if client is not None and _agent_must_yield(client, device_id):
        print(
            "[SKIP] Manual override active (controlled by MCP), skipping auto-decision.",
            flush=True,
        )
        return

    previous = agent.current_state(device_id)
    needs_cooling = agent.climate_needs_cooling(reading, previous)
    decision = agent.decide(reading)
    line = (
        f"[DECIDE] {reading['device_id']} T={reading['dht11_temperature']} "
        f"H={reading['dht11_humidity']} cool={needs_cooling} "
        f"motion={bool(reading.get('pir_motion', False))} "
        f"mq={bool(reading.get('mq_triggered', False))} -> {decision.command} "
        f"({decision.source}: {decision.reason})"
    )
    if decision.advice:
        line += f" | llm {decision.advice}"
    print(line, flush=True)
    if agent.dry_run:
        return
    try:
        _push_command(client, reading, decision)
    except TransportError as exc:
        print(f"[WARN] failed to push command: {exc}", file=sys.stderr)


def run(args: argparse.Namespace) -> None:
    chat_client = OllamaClient(args.ollama)
    agent = Agent(
        chat_client,
        model=args.model,
        min_switch_seconds=args.min_switch_seconds,
        dry_run=args.dry_run,
    )
    print(
        f"[INIT] hybrid mode model={args.model} "
        f"min_switch_seconds={args.min_switch_seconds}",
        flush=True,
    )
    backoff = 1
    try:
        with TransportClient(base_url=args.base, token=args.token) as client:
            while True:
                try:
                    consume_stream(client, agent)
                    backoff = 1
                except KeyboardInterrupt:
                    raise
                except TransportError as exc:
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
    parser.add_argument("--model", default="qwen2.5-coder:3b", help="Ollama model name")
    parser.add_argument("--token", default=None, help="shared token (X-Device-Token)")
    parser.add_argument(
        "--min-switch-seconds",
        type=float,
        default=30.0,
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
