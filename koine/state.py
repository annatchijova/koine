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

Two more states describe blocks on the *translation* side, which the loop
over source blocks structurally cannot see: a translated file can grow
content the source never had, and appending to it edits no recorded block,
so nothing above fires. UNSOURCED is that content; LEGACY_ORPHAN is the
subset of it that already existed when the pair was adopted.

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
UNSOURCED = "UNSOURCED"
LEGACY_ORPHAN = "LEGACY_ORPHAN"

# block kinds that are never eligible for translation (see blocks.split_blocks)
NEVER_TRANSLATED_KINDS = ("fence", "frontmatter")


@dataclass
class BlockState:
    doc: str
    lang: str
    block_index: int
    state: str
    detail: str = ""
    # which file block_index refers to. UNSOURCED/LEGACY_ORPHAN are indices
    # into the translated file; every other state indexes the source.
    side: str = "source"


# ops that carry a (source block → translation block) placement. "translate"
# records a new translation; "remap" restates an existing one whose position in
# the translated file shifted; "mirror" records a never-translated block copied
# verbatim. All three are placement facts, so all three are folded here.
_PLACEMENT_OPS = ("translate", "remap", "mirror")


def latest_events(ledger: Ledger) -> dict:
    """(doc, lang, block_index) → last placement event.

    Entries that did not parse carry no payload; they are already reported by
    `ledger.verify`, and skipping them here keeps a damaged chain from turning
    into a traceback halfway through state derivation.
    """
    out = {}
    for e in ledger.entries():
        p = e.get("payload")
        if p and p.get("op") in _PLACEMENT_OPS:
            out[(p["doc"], p["lang"], p["block_index"])] = e
    return out


def adopted_orphan_hashes(ledger: Ledger, doc: str, lang: str) -> set[str]:
    """Content hashes of translation blocks that had no source counterpart
    when the pair was adopted — the orphans koine already knew about."""
    out = set()
    for e in ledger.entries():
        p = e.get("payload")
        if p and p.get("op") == "adopt_orphan" and p["doc"] == doc and p["lang"] == lang:
            out.add(p["translation_hash"])
    return out


def block_mapping(ledger: Ledger, doc: str, lang: str) -> dict[int, int]:
    """source block index → translation block index, for one (doc, lang).

    The translated file is *not* required to be index-aligned with the source:
    an adopted translation may carry a banner the source lacks, or omit a
    section. Writing a new translation at the source's own index would then
    silently overwrite an unrelated block — so placement is read from the
    ledger, never assumed.
    """
    out: dict[int, int] = {}
    for (d, l, idx), e in latest_events(ledger).items():
        if d == doc and l == lang:
            meta = e["payload"].get("meta", {})
            out[idx] = meta.get("translation_block_index", idx)
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
        if b.kind in NEVER_TRANSLATED_KINDS:
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

    states.extend(_unsourced_states(doc, lang, tr_blocks, ledger))
    return states


def _unsourced_states(doc: str, lang: str, tr_blocks: list, ledger: Ledger
                      ) -> list[BlockState]:
    """Translation blocks that no source block is mapped to.

    The source-block loop can only report on content the source has. Content
    *appended* to a translated file edits no recorded block, so every check
    above passes and the gate goes green over a paragraph the source never
    said. This is the pass that sees it.

    Silence has one honest precondition: koine must already have records for
    this pair. With no placement events at all — a translation that was never
    adopted or translated — koine knows nothing about the file and calling its
    contents unsourced would be a claim it cannot back.
    """
    mapped = set(block_mapping(ledger, doc, lang).values())
    if not mapped:
        return []

    known_orphans = adopted_orphan_hashes(ledger, doc, lang)
    out: list[BlockState] = []
    for tb in tr_blocks:
        if tb.index in mapped:
            continue
        if seal_text(tb.raw) in known_orphans:
            out.append(BlockState(
                doc, lang, tb.index, LEGACY_ORPHAN, side="translation",
                detail="orphan present at adoption; no source counterpart"))
        else:
            out.append(BlockState(
                doc, lang, tb.index, UNSOURCED, side="translation",
                detail="no source block maps to it, and it is not an orphan "
                       "recorded at adoption — added or edited afterwards"))
    return out
