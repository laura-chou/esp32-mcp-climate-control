"""Tests for climate_agent decision logic.

The Ollama HTTP call is replaced by a tiny fake that returns canned strings;
everything else (prompt building, JSON parsing, fallback, anti-flapping guard)
runs as real code.
"""

from __future__ import annotations

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


def make_reading(temperature=30.0, humidity=55.0, device_id="esp32-climate-01"):
    return {
        "device_id": device_id,
        "dht11_temperature": temperature,
        "dht11_humidity": humidity,
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
    assert "T=30.0C>=28.0C" in decision.reason
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
