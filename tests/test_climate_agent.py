"""Tests for climate_agent decision logic.

The Ollama HTTP call is replaced by a tiny fake that returns canned strings;
everything else (prompt building, JSON parsing, fallback, anti-flapping guard)
runs as real code.
"""

from __future__ import annotations

import time

import pytest

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


def test_advisor_mode_ignores_model_command_and_keeps_real_numbers():
    # The model wrongly says OFF with a contradictory reason at 30C/75%.
    chat = FakeChat(['{"command": "OFF", "reason": "humidity is below 65%"}'])
    agent = Agent(chat, model="qwen2.5-coder:3b", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=30.0, humidity=75.0))
    # Rule engine decides; the model's bogus command never wins.
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    # The authoritative reason is built from the real numbers, not the model text.
    assert "H=75.0%" in decision.reason
    assert "below 65" not in decision.reason
    # The model's advice is still surfaced for observability.
    assert "DISAGREES" in decision.advice
    # Prompt still carries the current state to the model.
    _, messages = chat.calls[0]
    assert "current_fan: OFF" in messages[-1]["content"]


def test_advisor_mode_reason_matches_reading_on_threshold():
    # 28.0 C is exactly on temp_on_c; the rule engine opens the fan.
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="qwen2.5-coder:3b", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=28.0, humidity=68.0))
    assert decision.command == "ON"
    # the authoritative reason carries the real numbers
    assert "T=28.0C" in decision.reason
    assert "H=68.0%" in decision.reason


def test_rules_mode_never_calls_the_model():
    chat = FakeChat([])  # any call would IndexError
    agent = Agent(chat, model="qwen2.5-coder:3b", min_switch_seconds=0, mode="rules")
    decision = agent.decide(make_reading(temperature=30.0, humidity=55.0))
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    assert decision.advice == ""
    assert chat.calls == []


def test_llm_mode_uses_model_decision_with_honest_reason():
    chat = FakeChat(['{"command": "ON", "reason": "warm and humid"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0, mode="llm")
    decision = agent.decide(make_reading(temperature=30.0))
    assert decision.command == "ON"
    assert decision.source == "climate_agent"
    # reason is generated from the numbers, not the model's free text
    assert "T=30.0C" in decision.reason
    assert "needs cooling" in decision.reason
    assert "warm and humid" not in decision.reason
    assert agent.current_state("esp32-climate-01") == "ON"


def test_llm_mode_falls_back_to_rules_on_error():
    chat = FakeChat([RuntimeError("ollama down")])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0, mode="llm")
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-engine"
    assert decision.command == "ON"


def test_llm_mode_falls_back_on_unparseable_reply():
    chat = FakeChat(["I am not sure, sorry."])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0, mode="llm")
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-engine"
    assert decision.command == "ON"


def test_guard_holds_non_urgent_flip(monkeypatch):
    chat = FakeChat(['{"command": "ON", "reason": "flip"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0, mode="llm")
    # Establish a baseline: first switch records the timestamp.
    first = agent.decide(make_reading(temperature=29.0))
    assert first.command == "ON"

    # A second, non-urgent flip back to OFF right away must be suppressed.
    chat.replies.append('{"command": "OFF", "reason": "flip back"}')
    second = agent.decide(make_reading(temperature=27.0))
    assert second.command == "ON"
    assert "holding" in second.reason


def test_guard_allows_urgent_flip():
    chat = FakeChat(['{"command": "OFF", "reason": "off"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0, mode="llm")
    agent.remember("esp32-climate-01", "ON")

    # Very hot reading -> urgent -> the OFF flip is applied despite the guard.
    chat.replies.append('{"command": "OFF", "reason": "override"}')
    decision = agent.decide(make_reading(temperature=40.0))
    assert decision.command == "OFF"


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        Agent(FakeChat([]), model="x", mode="nonsense")


def test_module_is_importable_without_ollama():
    # Importing the module must not require an Ollama server or network.
    assert hasattr(climate_agent, "main")
    assert climate_agent.DEFAULT_OLLAMA.endswith(":11434")


def test_prompt_carries_pir_and_mq():
    chat = FakeChat(['{"command": "ON", "reason": "x"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    agent.decide(make_reading(temperature=25.0, humidity=50.0, pir_motion=True, mq_triggered=False))
    _, messages = chat.calls[0]
    content = messages[-1]["content"]
    assert "pir_motion: True" in content
    assert "mq_triggered: False" in content
    # the system prompt teaches the multimodal policy
    assert "MQ-135" in messages[0]["content"]


def test_mq_interlock_skips_the_model_entirely():
    # A triggered MQ-135 is handled by the safety interlock; the model is never
    # consulted, so no advice is recorded.
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=24.0, humidity=45.0, mq_triggered=True))
    assert decision.command == "ON"
    assert decision.advice == ""
    assert chat.calls == []


def test_mq_trigger_forces_on_and_reason_is_honest():
    # Model says OFF, but a triggered MQ-135 must ventilate.
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(
        make_reading(temperature=24.0, humidity=45.0, pir_motion=False, mq_triggered=True)
    )
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    assert "MQ-135" in decision.reason


def test_unoccupied_comfortable_keeps_fan_off():
    chat = FakeChat(['{"command": "ON", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0)
    decision = agent.decide(
        make_reading(temperature=24.0, humidity=45.0, pir_motion=False, mq_triggered=False)
    )
    assert decision.command == "OFF"
    assert "unoccupied" in decision.reason


def test_mq_trigger_counts_as_urgent():
    # A triggered MQ-135 must be treated as urgent so ventilation is never
    # blocked by the anti-flapping guard.
    agent = Agent(FakeChat([]), model="m", min_switch_seconds=30.0)
    comfortable = make_reading(temperature=25.0, humidity=50.0, mq_triggered=False)
    bad_air = make_reading(temperature=25.0, humidity=50.0, mq_triggered=True)
    assert agent._is_urgent(comfortable) is False
    assert agent._is_urgent(bad_air) is True


def test_mq_ventilation_is_applied_despite_recent_switch():
    # Establish a switch, then a bad-air reading within the guard window must
    # still turn the fan ON (urgent), where a comfort-only flip would be held.
    chat = FakeChat(['{"command": "ON", "reason": "flip"}'])
    agent = Agent(chat, model="m", min_switch_seconds=30.0, mode="llm")
    agent.remember("esp32-climate-01", "OFF")
    # Model says ON for a comfort reading -> allowed as the first switch.
    first = agent.decide(make_reading(temperature=30.0, humidity=50.0))
    assert first.command == "ON"
    # Now the model says OFF, but the air is bad: MQ urgency forces ON.
    chat.replies.append('{"command": "OFF", "reason": "off"}')
    bad = agent.decide(make_reading(temperature=25.0, humidity=50.0, mq_triggered=True))
    assert bad.command == "ON"
    assert "MQ-135" in bad.reason


# ---------------------------------------------------------------------------
# hybrid mode: rules precompute the climate flag, the model weighs PIR
# ---------------------------------------------------------------------------


def test_hybrid_prompt_passes_semantic_flags_not_raw_numbers():
    chat = FakeChat(['{"command": "ON", "reason": "x"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    # 29C -> needs cooling, but not extreme; unoccupied.
    agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=False))
    _, messages = chat.calls[0]
    content = messages[-1]["content"]
    assert "climate_needs_cooling: True" in content
    assert "climate_extreme: False" in content
    # raw numbers are deliberately withheld so the model cannot do arithmetic
    assert "dht11_temperature" not in content
    assert "29.0" not in content


def test_hybrid_model_may_save_energy_when_unoccupied():
    # needs_cooling True, not extreme, unoccupied -> the model may keep OFF.
    chat = FakeChat(['{"command": "OFF", "reason": "nobody here"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    decision = agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=False))
    assert decision.command == "OFF"
    assert decision.source == "climate_agent"
    assert "energy saving" in decision.reason


def test_hybrid_envelope_forces_on_when_cooling_and_occupied():
    # Same climate, but someone is present: the model cannot save energy here.
    chat = FakeChat(['{"command": "OFF", "reason": "save energy"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    decision = agent.decide(make_reading(temperature=29.0, humidity=50.0, pir_motion=True))
    assert decision.command == "ON"
    assert "needs cooling" in decision.reason


def test_hybrid_envelope_forces_on_when_extreme_even_unoccupied():
    chat = FakeChat(['{"command": "OFF", "reason": "save energy"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    # 35C is extreme -> the fan must run regardless of occupancy.
    decision = agent.decide(make_reading(temperature=35.0, humidity=50.0, pir_motion=False))
    assert decision.command == "ON"
    assert "extreme" in decision.reason


def test_hybrid_model_turns_off_when_comfortable():
    chat = FakeChat(['{"command": "OFF", "reason": "comfortable"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    decision = agent.decide(make_reading(temperature=25.0, humidity=50.0, pir_motion=True))
    assert decision.command == "OFF"
    assert "comfortable" in decision.reason


def test_hybrid_falls_back_to_rules_on_error():
    chat = FakeChat([RuntimeError("ollama down")])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    decision = agent.decide(make_reading(temperature=31.0, humidity=50.0))
    assert decision.source == "rule-engine"
    assert decision.command == "ON"


def test_hybrid_mq_interlock_skips_the_model():
    chat = FakeChat(['{"command": "OFF", "reason": "nope"}'])
    agent = Agent(chat, model="m", min_switch_seconds=0, mode="hybrid")
    decision = agent.decide(make_reading(temperature=24.0, humidity=45.0, mq_triggered=True))
    assert decision.command == "ON"
    assert decision.source == "rule-engine"
    assert chat.calls == []


def test_hybrid_keeps_min_switch_guard():
    chat = FakeChat(['{"command": "OFF", "reason": "flip"}'])
    agent = Agent(chat, model="m", min_switch_seconds=30.0, mode="hybrid")
    # Fan is ON (seeded); the room is comfortable so the model says OFF, but the
    # flip is non-urgent and within the guard window -> held ON.
    agent.remember("esp32-climate-01", "ON")
    agent._last_switch["esp32-climate-01"] = time.monotonic()
    decision = agent.decide(make_reading(temperature=25.0, humidity=50.0))
    assert decision.command == "ON"
    assert "holding" in decision.reason
