"""Fleet construction guards.

The orchestrator tests exercise the cycle with stub translate/review functions,
so they never touched the real ADK agents -- which is how a templating bug in an
agent instruction survived: ADK treats `{name}` in an instruction as a
session-state injection and raises KeyError on an unknown name, silently failing
every real translation. These guards pin the invariant that broke.
"""
import pytest

from koine.agents import fleet

INSTRUCTIONS = (
    "TRANSLATOR_INSTRUCTIONS",
    "REVIEWER_INSTRUCTIONS",
    "WATCHER_INSTRUCTIONS",
    "STEWARD_INSTRUCTIONS",
)


def test_no_agent_instruction_contains_a_brace_adk_would_template():
    for name in INSTRUCTIONS:
        text = getattr(fleet, name)
        assert "{" not in text and "}" not in text, (
            f"{name} contains a curly brace; ADK reads it as a session-state "
            f"template and raises KeyError, breaking every real translation")


def test_build_fleet_constructs_the_four_agents():
    pytest.importorskip("google.adk")  # only meaningful with the agents extra
    built = fleet.build_fleet()
    assert set(built) == {"watcher", "translator", "reviewer", "steward"}
