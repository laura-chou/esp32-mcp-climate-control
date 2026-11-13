"""climate_agent.py - the environment agent (AI brain).

This is the ``climate_agent.py`` box in the architecture diagram. It closes the
control loop:

    FastAPI /api/stream --(reading event)--> climate_agent
    climate_agent --(prompt)--> Ollama (Qwen2.5 / Llama3) --(decision)-->
    climate_agent --(POST /api/fan/command)--> FastAPI --> ESP32

It subscribes to the transport layer's SSE stream, asks a local Ollama model to
advise on every DHT11 reading, and pushes the resulting command back.

Decision safety
---------------
Small local models (e.g. ``qwen2.5-coder:3b``) are unreliable at exact numeric
comparisons: they hallucinate reasons that contradict the reading (e.g.
"humidity is below 65%" while it is 68%) and flap around a threshold, because
each token is sampled independently and the model has no hysteresis.

So by default the ON/OFF *decision* is made by the deterministic rule engine in
``server/rules.py`` (which has hysteresis and cannot contradict the numbers),
and the model only *advises* -- its free text is never used as the authoritative
reason. Modes (``--mode``):

    advisor  (default) rule engine decides; the model advises (logged only)
    rules              rule engine only; the model is not called at all
    llm                the model decides (use a capable model)

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
    "You are climate_agent, the multimodal environment control agent for a room. "
    "On every reading you receive: DHT11 temperature (Celsius) and relative "
    "humidity (percent), a PIR motion flag (whether someone is present) and an "
    "MQ-135 air-quality flag (whether the gas threshold is exceeded). "
    "Advise whether the cooling/ventilation fan should be ON or OFF, weighing: "
    "(1) comfort -- keep roughly 24-27 C, 40-60% humidity; "
    "(2) air quality -- if the MQ-135 threshold is exceeded, prefer ON to ventilate, "
    "regardless of temperature; "
    "(3) occupancy/energy -- when nobody is present (no motion), prefer OFF to save "
    "energy unless the room is clearly too hot, too humid, or the air is bad. "
    "Avoid rapid toggling: only change the current fan state when the reading "
    "clearly justifies it, otherwise keep the current state. "
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
    """Free-text advisory from the model (never the authoritative decision)."""

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
    """Turns readings into fan decisions.

    In ``advisor`` (default) and ``rules`` modes the deterministic rule engine
    decides, so the command can never contradict the reading and never flaps.
    In ``advisor`` mode the model is still consulted and its suggestion is
    logged; in ``llm`` mode the model decides.
    """

    def __init__(
        self,
        chat_client,
        model: str,
        min_switch_seconds: float = 15.0,
        mode: str = "advisor",
        settings: Settings = default_settings,
    ) -> None:
        if mode not in ("advisor", "rules", "llm"):
            raise ValueError(f"unknown mode: {mode!r}")
        self._chat = chat_client
        self._model = model
        self.mode = mode
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
            f"dht11_temperature: {reading['dht11_temperature']}\n"
            f"dht11_humidity: {reading['dht11_humidity']}\n"
            f"pir_motion: {bool(reading.get('pir_motion', False))}\n"
            f"mq_triggered: {bool(reading.get('mq_triggered', False))}\n"
            f"current_fan: {current}\n"
            f"comfort_band_c: {s.temp_off_c}-{s.temp_on_c}\n"
            f"humidity_band_pct: {s.humidity_off}-{s.humidity_on}\n"
            "Advise the fan command now."
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def _is_urgent(self, reading: dict) -> bool:
        """True when the reading is far enough outside the band to override
        the anti-flapping guard."""

        s = self._settings
        temperature = reading["dht11_temperature"]
        humidity = reading["dht11_humidity"]
        return (
            bool(reading.get("mq_triggered", False))
            or temperature >= s.temp_on_c + 3.0
            or humidity >= s.humidity_on + 5.0
            or temperature <= s.temp_off_c - 3.0
        )

    def _describe(self, command: str, reading: dict, current: str) -> str:
        """Render a factually-correct reason for ``command`` from the numbers.

        The model's free text is never used here, so the reason can never
        contradict the reading -- including when the model overrides the band.
        """

        s = self._settings
        t = reading["dht11_temperature"]
        h = reading["dht11_humidity"]
        motion = bool(reading.get("pir_motion", False))
        mq = bool(reading.get("mq_triggered", False))
        hot = t >= s.temp_on_c or h >= s.humidity_on
        cool = t <= s.temp_off_c and h <= s.humidity_off

        if command == "ON":
            if mq:
                return (
                    f"ON: MQ-135 air quality threshold exceeded -> ventilate "
                    f"(T={t:.1f}C, H={h:.1f}%)"
                )
            if current == "ON" and not cool:
                return (
                    f"holding ON: T={t:.1f}C / H={h:.1f}% still above the OFF "
                    f"threshold (need T<={s.temp_off_c}C and H<={s.humidity_off}%)"
                )
            if hot:
                triggers = []
                if t >= s.temp_on_c:
                    triggers.append(f"T={t:.1f}C>={s.temp_on_c}C")
                if h >= s.humidity_on:
                    triggers.append(f"H={h:.1f}%>={s.humidity_on}%")
                return "ON: " + " and ".join(triggers)
            return (
                f"ON (model override): within band (T={t:.1f}C<{s.temp_on_c}C, "
                f"H={h:.1f}%<{s.humidity_on}%)"
            )

        # command == "OFF"
        if current == "ON" and cool:
            suffix = " (unoccupied -> energy saving)" if not motion else ""
            return (
                f"OFF: cooled to T={t:.1f}C<={s.temp_off_c}C and "
                f"H={h:.1f}%<={s.humidity_off}%{suffix}"
            )
        if mq:
            return (
                f"OFF (model override): MQ-135 threshold still exceeded "
                f"(T={t:.1f}C, H={h:.1f}%) -- ventilation recommended"
            )
        if not hot:
            suffix = " and unoccupied" if not motion else ""
            return (
                f"OFF: within band (T={t:.1f}C<{s.temp_on_c}C, "
                f"H={h:.1f}%<{s.humidity_on}%){suffix}"
            )
        return (
            f"OFF (model override): T={t:.1f}C / H={h:.1f}% still above the ON "
            f"threshold (T>={s.temp_on_c}C or H>={s.humidity_on}%)"
        )

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
        """Ask the model for advice. Returns ``None`` if unusable/unreachable."""

        try:
            raw = self._chat.chat(self._model, self.build_messages(reading, current))
            parsed = parse_decision(raw)
        except Exception as exc:  # network error, bad JSON, missing keys, ...
            print(f"[WARN] ollama call failed: {exc}", file=sys.stderr)
            return None
        if parsed is None:
            return None
        command, reason = parsed
        return Advice(command, reason)

    def _decide_llm(self, reading: dict, current: str) -> Decision:
        advice = self._consult(reading, current)
        if advice is None:
            return self._rule_decision(reading, current)
        return Decision(
            advice.command,
            self._describe(advice.command, reading, current),
            "climate_agent",
            advice.reason,
        )

    def decide(self, reading: dict) -> Decision:
        device_id = reading["device_id"]
        current = self.current_state(device_id)

        # Safety interlock: a triggered MQ-135 forces ventilation in every mode
        # (even llm) so air quality can never be overruled by the model or the
        # comfort band.
        if bool(reading.get("mq_triggered", False)):
            decision = self._rule_decision(reading, current)
        elif self.mode == "rules":
            decision = self._rule_decision(reading, current)
        elif self.mode == "llm":
            decision = self._decide_llm(reading, current)
        else:  # advisor (default): rules decide, model advises
            decision = self._rule_decision(reading, current)
            advice = self._consult(reading, current)
            if advice is not None:
                agreement = "agrees" if advice.command == decision.command else "DISAGREES"
                decision.advice = f"{agreement}: {advice.command} ({advice.reason})"

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
                    decision.advice,
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
    line = (
        f"[DECIDE] {reading['device_id']} T={reading['dht11_temperature']} "
        f"H={reading['dht11_humidity']} "
        f"motion={bool(reading.get('pir_motion', False))} "
        f"mq={bool(reading.get('mq_triggered', False))} -> {decision.command} "
        f"({decision.source}: {decision.reason})"
    )
    if decision.advice:
        line += f" | llm {decision.advice}"
    print(line, flush=True)
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
        mode=args.mode,
    )
    print(
        f"[INIT] mode={args.mode} model={args.model} "
        f"min_switch_seconds={args.min_switch_seconds}",
        flush=True,
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
    parser.add_argument("--model", default="qwen2.5-coder:3b", help="Ollama model name")
    parser.add_argument("--token", default=None, help="shared token (X-Device-Token)")
    parser.add_argument(
        "--mode",
        choices=("advisor", "rules", "llm"),
        default="advisor",
        help=(
            "advisor: rule engine decides, model advises (default); "
            "rules: rule engine only, no model calls; "
            "llm: model decides (needs a capable model)"
        ),
    )
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
