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


def test_agent_uses_model_decision():
    chat = FakeChat(['{"command": "ON", "reason": "warm and humid"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=30.0))
    assert decision.command == "ON"
    assert decision.source == "climate_agent"
    assert agent.current_state("esp32-climate-01") == "ON"
    # the prompt carried the current state to the model
    _, messages = chat.calls[0]
    assert "current_fan: OFF" in messages[-1]["content"]


def test_agent_falls_back_to_rules_on_error():
    chat = FakeChat([RuntimeError("ollama down")])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-fallback"
    assert decision.command == "ON"


def test_agent_falls_back_on_unparseable_reply():
    chat = FakeChat(["I am not sure, sorry."])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=0)
    decision = agent.decide(make_reading(temperature=31.0, humidity=75.0))
    assert decision.source == "rule-fallback"
    assert decision.command == "ON"


def test_guard_holds_non_urgent_flip(monkeypatch):
    chat = FakeChat(['{"command": "ON", "reason": "flip"}'])
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0)
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
    agent = Agent(chat, model="qwen2.5", min_switch_seconds=30.0)
    agent.remember("esp32-climate-01", "ON")

    # Very hot reading -> urgent -> the OFF flip is applied despite the guard.
    chat.replies.append('{"command": "OFF", "reason": "override"}')
    decision = agent.decide(make_reading(temperature=40.0))
    assert decision.command == "OFF"


def test_module_is_importable_without_ollama():
    # Importing the module must not require an Ollama server or network.
    assert hasattr(climate_agent, "main")
    assert climate_agent.DEFAULT_OLLAMA.endswith(":11434")
