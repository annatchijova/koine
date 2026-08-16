"""Mechanical derivation of translation state. No model anywhere in here.

For each (doc, lang, block) the state is derived from hashes alone:

  CURRENT       ledger's latest translation event for the block references
                the block's *current* source hash, and the translated file's
                block content matches the recorded translation hash
  STALE         the source block changed after the last translation event
  TAMPERED      the translated file no longer matches its recorded hash
                (someone edited the translation outside the pipeline)
  UNTRANSLATED  no ledger event for this block/lang
  MACHINE_ONLY  same as CURRENT but the event was flagged unreviewed —
                surfaced honestly to readers, never hidden

A language model may *cause* these states to change by producing candidate
translations; it can never *declare* a state. That is the whole point.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .blocks import split_blocks
from .canonical import seal_text
from .ledger import Ledger

CURRENT = "CURRENT"
STALE = "STALE"
TAMPERED = "TAMPERED"
UNTRANSLATED = "UNTRANSLATED"
MACHINE_ONLY = "MACHINE_ONLY"
LEGACY_UNVERIFIED = "LEGACY_UNVERIFIED"
NOT_TRANSLATABLE = "NOT_TRANSLATABLE"

# block kinds that are never eligible for translation (see blocks.split_blocks)
_NEVER_TRANSLATED_KINDS = ("fence", "frontmatter")


@dataclass
class BlockState:
    doc: str
    lang: str
    block_index: int
    state: str
    detail: str = ""


def latest_events(ledger: Ledger) -> dict:
    """(doc, lang, block_index) → last 'translate' event."""
    out = {}
    for e in ledger.entries():
        p = e["payload"]
        if p["op"] == "translate":
            out[(p["doc"], p["lang"], p["block_index"])] = e
    return out


def derive_states(source_path: str | Path, translation_path: str | Path,
                  lang: str, ledger: Ledger) -> list[BlockState]:
    source_path = Path(source_path)
    translation_path = Path(translation_path)
    doc = source_path.as_posix()

    src_blocks = split_blocks(source_path.read_text(encoding="utf-8"))
    tr_blocks = (
        split_blocks(translation_path.read_text(encoding="utf-8"))
        if translation_path.exists() else []
    )
    events = latest_events(ledger)

    states: list[BlockState] = []
    for b in src_blocks:
        if b.kind in _NEVER_TRANSLATED_KINDS:
            states.append(BlockState(
                doc, lang, b.index, NOT_TRANSLATABLE,
                detail=f"{b.kind}, never eligible for translation"))
            continue

        key = (doc, lang, b.index)
        ev = events.get(key)
        if ev is None:
            states.append(BlockState(doc, lang, b.index, UNTRANSLATED))
            continue
        p = ev["payload"]
        if p["source_hash"] != b.source_hash:
            states.append(BlockState(
                doc, lang, b.index, STALE,
                detail="source changed after last translation"))
            continue
        meta = p.get("meta", {})
        tr_index = meta.get("translation_block_index", b.index)
        if tr_index >= len(tr_blocks):
            states.append(BlockState(
                doc, lang, b.index, TAMPERED,
                detail="translated file has fewer blocks than recorded"))
            continue
        actual = seal_text(tr_blocks[tr_index].raw)
        if actual != p["translation_hash"]:
            states.append(BlockState(
                doc, lang, b.index, TAMPERED,
                detail="translated block edited outside the pipeline"))
            continue
        if meta.get("legacy"):
            states.append(BlockState(
                doc, lang, b.index, LEGACY_UNVERIFIED,
                detail="adopted alignment; meaning never verified"))
        elif meta.get("reviewed") is False:
            states.append(BlockState(doc, lang, b.index, MACHINE_ONLY))
        else:
            states.append(BlockState(doc, lang, b.index, CURRENT))
    return states
