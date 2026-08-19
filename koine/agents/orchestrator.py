"""Wires one translation cycle: watcher-selected blocks -> translator -> reviewer
-> mechanical submit. Model calls are injected as plain functions so this module
can be exercised in tests without an ADK runtime or a Gemini credential.
`run_with_adk` wires the real fleet from `fleet.build_fleet`; everything else
here is deterministic glue that never trusts a model's opinion beyond REJECT.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..glossary import Glossary
from ..ledger import Ledger
from ..state import STALE, UNTRANSLATED, derive_states
from . import contracts

# frozen template -> translated template
TranslateFn = Callable[[str], str]
# (source block text, candidate translated text) -> "NO_FINDINGS" or "REJECT: ..."
ReviewFn = Callable[[str, str], str]

NO_FINDINGS = "NO_FINDINGS"


@dataclass
class CycleResult:
    block_index: int
    lang: str
    outcome: str  # "promoted", "rejected", "skipped"
    detail: str = ""


def translate_one_block(*, source_path: str, translation_file: str, lang: str,
                        block_index: int, ledger: Ledger, glossary: Glossary,
                        translate_fn: TranslateFn, review_fn: ReviewFn) -> CycleResult:
    """One block through the full fleet. The model's words never promote
    anything directly: `contracts.submit_candidate` re-checks everything
    mechanically before the ledger is touched."""
    try:
        prep = contracts.prepare_block(source_path, block_index)
    except contracts.CandidateRejected as e:
        return CycleResult(block_index, lang, "skipped", detail="; ".join(e.reasons))

    candidate_template = translate_fn(prep["template"])

    verdict = review_fn(prep["block"].raw, candidate_template).strip()
    if verdict != NO_FINDINGS:
        return CycleResult(block_index, lang, "rejected", detail=verdict)

    try:
        contracts.submit_candidate(
            source_path=source_path, block_index=block_index, lang=lang,
            translated_template=candidate_template, ledger=ledger,
            glossary=glossary, reviewed=True, translation_file=translation_file,
        )
    except contracts.CandidateRejected as e:
        return CycleResult(block_index, lang, "rejected", detail="; ".join(e.reasons))

    return CycleResult(block_index, lang, "promoted")


def run_cycle(*, source_path: str, translation_file: str, lang: str,
             ledger: Ledger, glossary: Glossary,
             translate_fn: TranslateFn, review_fn: ReviewFn) -> list[CycleResult]:
    """One pass over every UNTRANSLATED/STALE block for `lang`, in block order.
    Each block is re-derived from the ledger before it is touched, so a
    rejection or a submit failure on block N never blocks block N+1."""
    # code fences and frontmatter first: copied verbatim, so the translated
    # file carries them and every later block lands at a known position
    contracts.mirror_untranslatable(
        source_path=source_path, translation_file=translation_file,
        lang=lang, ledger=ledger)

    states = derive_states(source_path, translation_file, lang, ledger)
    results = []
    for s in states:
        if s.state in (UNTRANSLATED, STALE):
            results.append(translate_one_block(
                source_path=source_path, translation_file=translation_file,
                lang=lang, block_index=s.block_index, ledger=ledger,
                glossary=glossary, translate_fn=translate_fn, review_fn=review_fn,
            ))
    return results


def run_with_adk(*, source_path: str, translation_file: str, lang: str,
                 source_lang: str, ledger: Ledger, glossary: Glossary,
                 model: str = "gemini-3.5-flash") -> list[CycleResult]:
    """Production entry point: wires the real ADK translator/reviewer agents
    from `fleet.build_fleet` as translate_fn/review_fn, then delegates to the
    same `run_cycle` the tests exercise with stub functions."""
    from . import fleet as fleet_mod

    built = fleet_mod.build_fleet(model=model)
    translate_fn = fleet_mod.make_translate_fn(
        built["translator"], source_lang=source_lang, target_lang=lang)
    review_fn = fleet_mod.make_review_fn(built["reviewer"])

    return run_cycle(
        source_path=source_path, translation_file=translation_file, lang=lang,
        ledger=ledger, glossary=glossary,
        translate_fn=translate_fn, review_fn=review_fn,
    )
