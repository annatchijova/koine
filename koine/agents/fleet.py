"""The ADK fleet. Four agents, disjoint tools, none in the trust path.

Requires `google-adk` and a Gemini credential. Without them, importing this
module still works and `build_fleet()` raises a clear error naming what is
missing — honest degradation, not a stack trace from deep inside a library.

  watcher      reads block states, decides WHAT needs translating first
  translator   receives a frozen template, returns a translated template
  reviewer     adversarial reading of a candidate; can only REJECT
  steward      spots recurring terms, PROPOSES glossary entries

The prompts are explicit that placeholders (⟦Kn⟧) are inviolable and that
the agents' words never decide anything: promotion is mechanical.
"""
from __future__ import annotations

import asyncio
import uuid

from . import contracts

TRANSLATOR_INSTRUCTIONS = """\
You translate a single markdown block from {source_lang} to {target_lang}.
The text contains placeholders like ⟦K0⟧, ⟦K1⟧. They stand for code, URLs,
flags, and paths. You MUST keep every placeholder exactly as-is, each
exactly once. Do not add, drop, or renumber them. Translate the prose
around them naturally, preserving markdown structure (headings stay
headings, list items stay list items). Return ONLY the translated template,
no commentary.
"""

REVIEWER_INSTRUCTIONS = """\
You are an adversarial reviewer of a candidate translation. You will see the
source block and the candidate. Look for: meaning drift, negations lost,
numbers changed, hedges dropped, terminology inconsistency. You can only
REJECT with specific findings, or stay silent. You cannot approve — approval
is not yours to give; mechanical checks decide promotion. If you find
nothing, reply exactly: NO_FINDINGS.
"""

WATCHER_INSTRUCTIONS = """\
You prioritize translation work. Given block states (STALE, UNTRANSLATED,
MACHINE_ONLY) for each language, produce an ordered task list. Prioritize:
1) STALE blocks in reviewed languages (readers are being lied to),
2) UNTRANSLATED blocks in high-traffic docs, 3) MACHINE_ONLY upgrades.
You only order work; you never mark anything current.
"""

STEWARD_INSTRUCTIONS = """\
You watch translated blocks for recurring domain terms rendered
inconsistently. When you spot one, PROPOSE a glossary entry with the term
and one rendering per language, plus a one-line rationale. A human binds
entries; you never do.
"""


def build_fleet(model: str = "gemini-3.5-flash"):
    try:
        from google.adk.agents import Agent  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "google-adk is not installed; the deterministic pipeline "
            "(blocks, ledger, gate) works without it — only the agents "
            "need it. pip install google-adk"
        ) from e

    watcher = Agent(
        name="watcher", model=model, instruction=WATCHER_INSTRUCTIONS,
        tools=[contracts.list_blocks],
    )
    translator = Agent(
        name="translator", model=model, instruction=TRANSLATOR_INSTRUCTIONS,
        tools=[],  # receives the frozen template as input; submits nothing itself
    )
    reviewer = Agent(
        name="reviewer", model=model, instruction=REVIEWER_INSTRUCTIONS,
        tools=[contracts.record_rejection],
    )
    steward = Agent(
        name="steward", model=model, instruction=STEWARD_INSTRUCTIONS,
        tools=[],
    )
    return {"watcher": watcher, "translator": translator,
            "reviewer": reviewer, "steward": steward}


def _run_agent_once(agent, user_text: str) -> str:
    """Send one message to an ADK agent in a fresh session, return the final
    response text. Blocking/synchronous: appropriate for the one-block-at-a-
    time cadence of `orchestrator.run_cycle`, not for high-throughput use."""
    from google.adk.runners import Runner  # type: ignore
    from google.adk.sessions import InMemorySessionService  # type: ignore
    from google.genai import types  # type: ignore

    app_name = f"koine-{agent.name}"
    user_id = "koine"
    session_id = str(uuid.uuid4())
    session_service = InMemorySessionService()
    asyncio.run(session_service.create_session(
        app_name=app_name, user_id=user_id, session_id=session_id))

    runner = Runner(agent=agent, app_name=app_name,
                    session_service=session_service)
    message = types.Content(role="user", parts=[types.Part(text=user_text)])

    final_text = ""
    for event in runner.run(user_id=user_id, session_id=session_id,
                            new_message=message):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)
    return final_text


def make_translate_fn(translator_agent, *, source_lang: str, target_lang: str):
    """frozen template -> translated template, via the ADK translator agent.
    The instruction already fixes the placeholder contract; this only fills
    in the language pair per call."""
    def translate_fn(template: str) -> str:
        prompt = (f"Source language: {source_lang}\n"
                 f"Target language: {target_lang}\n\n{template}")
        return _run_agent_once(translator_agent, prompt)
    return translate_fn


def make_review_fn(reviewer_agent):
    """(source block, candidate) -> "NO_FINDINGS" or "REJECT: ..."."""
    def review_fn(source_block: str, candidate: str) -> str:
        prompt = f"SOURCE:\n{source_block}\n\nCANDIDATE:\n{candidate}"
        return _run_agent_once(reviewer_agent, prompt)
    return review_fn
