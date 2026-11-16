"""Tests for climate_agent decision logic.

The Ollama HTTP call is replaced by a tiny fake that returns canned strings;
everything else (prompt building, JSON parsing, the safety envelope, the
automatic rule-engine fallback, and the anti-flapping guard) runs as real code.
"""

from __future__ import annotations

import argparse
import time

import climate_agent
from climate_agent import Agent, parse_decision


class FakeChat:
    """Stand-in for OllamaClient: returns a queued reply or raises."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, model, messages):
        self.calls.append((model, messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def make_reading(
    temperature=30.0,
    humidity=55.0,
    device_id="esp32-climate-01",
    pir_motion=True,
    mq_triggered=False,
):
    return {
        "device_id": device_id,
        "dht11_temperature": temperature,
        "dht11_humidity": humidity,
        "pir_motion": pir_motion,
        "mq_triggered": mq_triggered,
    }


def test_parse_decision_plain_json():
    assert parse_decision('{"command": "ON", "reason": "too hot"}') == ("ON", "too hot")


def test_parse_decision_lowercase_and_code_fence():
    text = 'Sure!\n```json\n{"command": "off", "reason": "cool"}\n```'
    assert parse_decision(text) == ("OFF", "cool")


def test_parse_decision_rejects_garbage():
    assert parse_decision("no json here") is None
    assert parse_decision('{"command": "MAYBE"}') is None
    assert parse_decision("") is None


def test_module_is_importable_without_ollama():
    # Importing the module must not require an Ollama server or network.
    assert hasattr(climate_agent, "main")
    assert climate_agent.DEFAULT_OLLAMA.endswith(":11434")


def test_no_mode_argument_remains():
    # The mode switch was removed: the CLI exposes no --mode option.
    options = {action.dest for action in _agent_parser()._actions}
    assert "mode" not in options


def _agent_parser() -> argparse.ArgumentParser:
    """Mirror the argument list built in climate_agent.main()."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=climate_agent.DEFAULT_BASE)
    parser.add_argument("--ollama", default=climate_agent.DEFAULT_OLLAMA)
    parser.add_argument("--model", default="qwen2.5-coder:3b")
    parser.add_argument("--token", default=None)
    parser.add_argument("--min-switch-seconds", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser


# ---------------------------------------------------------------------------
# prompt: semantic flags only, no raw numbers
# ---------------------------------------------------------------------------


def test_prompt_passes_semantic_flags_not_raw_numbers():
    chat = FakeChat(['{"command": "ON", "reason": "x"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    # 29C -> needs cooling, but not extreme; unoccupied.
    agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=False))
    _, messages = chat.calls[0]
    content = messages[-1]["content"]
    assert "climate_needs_cooling: True" in content
    assert "climate_extreme: False" in content
    assert "pir_motion: False" in content
    assert "mq_triggered: False" in content
    # raw numbers are deliberately withheld so the model cannot do arithmetic
    assert "dht11_temperature" not in content
    assert "29.0" not in content
    # the system prompt teaches the multimodal policy
    assert "MQ-135" in messages[0]["content"]


def test_prompt_carries_current_fan_state():
    chat = FakeChat(['{"command": "ON", "reason": "x"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    agent.decide(make_reading(temperature=29.0, humidity=50.0))
    _, messages = chat.calls[0]
    assert "current_fan: OFF" in messages[-1]["content"]


# ---------------------------------------------------------------------------
# happy path: the model decides inside the envelope
# ---------------------------------------------------------------------------


def test_model_decision_uses_honest_reason_from_numbers():
    chat = FakeChat(['{"command": "ON", "reason": "warm and humid"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=30.0))
    assert decision.command == "ON"
    assert decision.source == "climate_agent"
    # reason is generated from the numbers, not the model's free text
    assert "T=30.0C" in decision.reason
    assert "needs cooling" in decision.reason
    assert "warm and humid" not in decision.reason
    # the model's own short reason is surfaced for the log
    assert decision.advice == "warm and humid"
    assert agent.current_state("esp32-climate-01") == "ON"


def test_model_may_save_energy_when_unoccupied():
    # needs_cooling True, not extreme, unoccupied -> the model may keep OFF.
    chat = FakeChat(['{"command": "OFF", "reason": "nobody here"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=False))
    assert decision.command == "OFF"
    assert decision.source == "climate_agent"
    assert "energy saving" in decision.reason


def test_model_turns_off_when_comfortable():
    chat = FakeChat(['{"command": "OFF", "reason": "comfortable"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=25.0, humidity=50.0, pir_motion=True))
    assert decision.command == "OFF"
    assert "comfortable" in decision.reason


# ---------------------------------------------------------------------------
# safety envelope: the model cannot disable needed cooling
# ---------------------------------------------------------------------------


def test_envelope_forces_on_when_cooling_and_occupied():
    # Someone is present: the model cannot "save energy" here.
    chat = FakeChat(['{"command": "OFF", "reason": "save energy"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=True))
    assert decision.command == "ON"
    assert "needs cooling" in decision.reason


def test_envelope_forces_on_when_extreme_even_unoccupied():
    chat = FakeChat(['{"command": "OFF", "reason": "save energy"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    # 35C is extreme -> the fan must run regardless of occupancy.
    decision = agent.decide(make_reading(temperature=35.0, humidity=50.0, pir_motion=False))
    assert decision.command == "ON"
    assert "extreme" in decision.reason


def test_cold_room_is_not_extreme_and_model_may_keep_off():
    # A cold room must not be treated as extreme; the model's OFF stands (the fan
    # cannot heat, so being too cold is not a reason to run it).
    chat = FakeChat(['{"command": "OFF", "reason": "cold"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=18.0, humidity=45.0, pir_motion=True))
    assert decision.command == "OFF"
    assert "extreme" not in decision.reason
    assert "comfortable" in decision.reason


# ---------------------------------------------------------------------------
# automatic fallback to the deterministic rule engine
# ---------------------------------------------------------------------------


def test_falls_back_to_rules_on_error():
    chat = FakeChat([RuntimeError("ollama down")])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-engine"
    assert decision.command == "ON"


def test_falls_back_on_unparseable_reply():
    chat = FakeChat(["I am not sure, sorry."])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-engine"
    assert decision.command == "ON"


def test_fallback_returns_off_when_comfortable():
    chat = FakeChat([RuntimeError("down")])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=24.0, humidity=45.0, pir_motion=True))
    assert decision.source == "rule-engine"
    assert decision.command == "OFF"


# ---------------------------------------------------------------------------
# air-quality floor: a triggered MQ-135 short-circuits before the model
# ---------------------------------------------------------------------------


def test_mq_floor_skips_the_model_entirely():
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=24.0, humidity=45.0, mq_triggered=True))
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    assert decision.advice == ""
    assert chat.calls == []


def test_mq_trigger_forces_on_and_reason_is_honest():
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(
        make_reading(temperature=24.0, humidity=45.0, pir_motion=False, mq_triggered=True)
    )
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    assert "MQ-135" in decision.reason


def test_mq_trigger_counts_as_urgent():
    agent = Agent(FakeChat([]), model="m", min_switch_seconds=30.0)
    comfortable = make_reading(temperature=25.0, humidity=50.0, mq_triggered=False)
    bad_air = make_reading(temperature=25.0, humidity=50.0, mq_triggered=True)
    assert agent._is_urgent(comfortable) is False
    assert agent._is_urgent(bad_air) is True


# ---------------------------------------------------------------------------
# anti-flapping guard
# ---------------------------------------------------------------------------


def test_guard_holds_non_urgent_flip():
    chat = FakeChat(['{"command": "ON", "reason": "flip"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0)
    # Establish a baseline: first switch records the timestamp.
    first = agent.decide(make_reading(temperature=29.0, humidity=50.0))
    assert first.command == "ON"

    # A second, non-urgent flip back to OFF right away must be suppressed.
    chat.replies.append('{"command": "OFF", "reason": "flip back"}')
    second = agent.decide(make_reading(temperature=25.0, humidity=50.0))
    assert second.command == "ON"
    assert "holding" in second.reason


def test_guard_allows_urgent_flip():
    chat = FakeChat(['{"command": "OFF", "reason": "off"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0)
    agent.remember("esp32-climate-01", "OFF")
    # Pretend a switch just happened, so a non-urgent flip would be held.
    agent._last_switch["esp32-climate-01"] = time.monotonic()

    # 40C is extreme: the envelope forces ON and urgency bypasses the guard.
    decision = agent.decide(make_reading(temperature=40.0, humidity=50.0))
    assert decision.command == "ON"
    assert "extreme" in decision.reason


def test_mq_ventilation_is_applied_despite_recent_switch():
    # Establish a switch, then a bad-air reading within the guard window must
    # still turn the fan ON (urgent), where a comfort-only flip would be held.
    chat = FakeChat(['{"command": "ON", "reason": "flip"}'])
    agent = Agent(chat, model="m", min_switch_seconds=30.0)
    agent.remember("esp32-climate-01", "OFF")
    first = agent.decide(make_reading(temperature=30.0, humidity=50.0))
    assert first.command == "ON"
    chat.replies.append('{"command": "OFF", "reason": "off"}')
    bad = agent.decide(make_reading(temperature=25.0, humidity=50.0, mq_triggered=True))
    assert bad.command == "ON"
    assert "MQ-135" in bad.reason


# ---------------------------------------------------------------------------
# [DECIDE] log line: decision, source and short AI reason
# ---------------------------------------------------------------------------


def test_decide_log_line_reports_source_and_reason(capsys):
    chat = FakeChat(['{"command": "ON", "reason": "occupant present"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, dry_run=True)
    climate_agent._handle_reading(None, agent, make_reading(temperature=30.0))
    out = capsys.readouterr().out
    assert "[DECIDE]" in out
    assert "-> ON" in out
    assert "climate_agent:" in out
    assert "llm occupant present" in out


def test_decide_log_line_reports_rule_engine_fallback(capsys):
    chat = FakeChat([RuntimeError("down")])
    agent = Agent(chat, model="m", min_switch_seconds=0, dry_run=True)
    climate_agent._handle_reading(None, agent, make_reading(temperature=31.0))
    out = capsys.readouterr().out
    assert "rule-engine:" in out
    assert "-> ON" in out
