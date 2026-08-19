"""Tool contracts for the fleet. Strictly disjoint by design.

The judged property is separation of responsibilities. Each agent gets a
closed set of tools; no tool anywhere lets a model write a state, a hash,
or a ledger entry directly. The pipeline functions below are the ONLY
writers, and they run mechanical checks before touching the ledger.

  watcher     read-only: list block states, read glossary → opens tasks
  translator  freeze → (model translates the template) → submit candidate
  reviewer    read candidate + source; may only REJECT with a reason.
              It cannot approve: absence of rejection + mechanical checks
              passing is what promotes a candidate. An LLM "approval" would
              put a model back into the trust path.
  steward     may only PROPOSE glossary entries; binding them is human.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from ..blocks import Block, freeze, split_blocks, thaw, verify_protected_spans
from ..canonical import seal_text
from ..glossary import Glossary
from ..ledger import Ledger
from ..state import NEVER_TRANSLATED_KINDS, block_mapping, latest_events


class CandidateRejected(Exception):
    """Mechanical rejection. Carries machine-checkable reasons."""
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# ---------- watcher tools (read-only) ----------

def list_blocks(source_path: str) -> list[dict]:
    return [
        {"index": b.index, "kind": b.kind, "source_hash": b.source_hash,
         "preview": b.raw[:80]}
        for b in split_blocks(Path(source_path).read_text(encoding="utf-8"))
    ]


# ---------- translator pipeline ----------

def prepare_block(source_path: str, block_index: int) -> dict:
    """Freeze a block for translation. What the model receives is the
    template with placeholders — never the protected spans themselves."""
    blocks = split_blocks(Path(source_path).read_text(encoding="utf-8"))
    b = blocks[block_index]
    if b.kind in NEVER_TRANSLATED_KINDS:
        raise CandidateRejected([f"{b.kind} blocks are never translated"])
    frozen = freeze(b.raw)
    return {"block": b, "frozen": frozen, "template": frozen.template}


# ---------- placement (mechanical, no model) ----------

def _read_raws(translation_file: str) -> list[str]:
    p = Path(translation_file)
    if not p.exists():
        return []
    return [b.raw for b in split_blocks(p.read_text(encoding="utf-8"))]


def _write_raws(translation_file: str, raws: list[str]) -> None:
    Path(translation_file).write_text("\n\n".join(raws) + "\n", encoding="utf-8")


def _place(raws: list[str], mapping: dict, block_index: int,
           text: str) -> tuple[int, dict]:
    """Put *text* in *raws* at the slot belonging to source block *block_index*.

    Returns (target index, {source index: new translation index}) for every
    already-placed block whose translation index moved. Mutates *raws*.

    Placement never guesses: an existing mapping is reused, and a new block is
    inserted directly after the translation of the nearest preceding source
    block. Identity mapping falls out of this for a translation built from
    scratch, without being assumed for one that was adopted.
    """
    target = mapping.get(block_index)
    if target is not None:
        if target >= len(raws):
            raise CandidateRejected([
                f"recorded translation block {target} is missing from the file "
                f"({len(raws)} blocks); the translation lost content — "
                f"restore it or re-adopt, koine will not write over the gap"
            ])
        owner = [i for i, j in mapping.items() if j == target and i != block_index]
        if owner:
            raise CandidateRejected([
                f"translation block {target} is also mapped from source block "
                f"{owner[0]}; refusing to overwrite another block's translation"
            ])
        raws[target] = text
        return target, {}

    preceding = [j for i, j in mapping.items() if i < block_index]
    target = min(max(preceding) + 1 if preceding else 0, len(raws))
    raws.insert(target, text)
    shifted = {i: j + 1 for i, j in mapping.items() if j >= target}
    return target, shifted


def _record_shift(ledger: Ledger, doc: str, lang: str, shifted: dict,
                  seq_time: str) -> None:
    """Append a `remap` event per block whose translation index moved.

    The ledger is append-only, so a shift is recorded, never rewritten: the
    hashes are copied verbatim from the block's previous event and only the
    placement changes. `state.latest_events` folds `remap` like `translate`.
    """
    if not shifted:
        return
    events = latest_events(ledger)
    for src_idx in sorted(shifted):
        prev = events.get((doc, lang, src_idx))
        if prev is None:
            continue
        p = prev["payload"]
        meta = dict(p.get("meta", {}))
        meta["translation_block_index"] = shifted[src_idx]
        ledger.append(
            op="remap", doc=doc, lang=lang, block_index=src_idx,
            source_hash=p["source_hash"], translation_hash=p["translation_hash"],
            seq_time=seq_time, meta=meta,
        )


def mirror_untranslatable(*, source_path: str, translation_file: str, lang: str,
                          ledger: Ledger) -> list[int]:
    """Copy code fences and frontmatter into the translation, byte-identical.

    These blocks are never translated, but a translated README without its
    code fences is not a translated README. Copying is mechanical and exact,
    so it needs no model and no review. Returns the source indices mirrored.
    """
    doc = Path(source_path).as_posix()
    src_blocks = split_blocks(Path(source_path).read_text(encoding="utf-8"))
    raws = _read_raws(translation_file)
    mapping = block_mapping(ledger, doc, lang)
    seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()

    mirrored: list[int] = []
    for b in src_blocks:
        if b.kind not in NEVER_TRANSLATED_KINDS:
            continue
        target = mapping.get(b.index)
        if target is not None and target < len(raws) and raws[target] == b.raw:
            continue  # already mirrored and unchanged
        target, shifted = _place(raws, mapping, b.index, b.raw)
        mapping[b.index] = target
        mapping.update(shifted)
        ledger.append(
            op="mirror", doc=doc, lang=lang, block_index=b.index,
            source_hash=b.source_hash, translation_hash=b.source_hash,
            seq_time=seq_time,
            meta={"kind": b.kind, "translation_block_index": target},
        )
        _record_shift(ledger, doc, lang, shifted, seq_time)
        mirrored.append(b.index)

    if mirrored:
        _write_raws(translation_file, raws)
    return mirrored


def submit_candidate(*, source_path: str, block_index: int, lang: str,
                     translated_template: str, ledger: Ledger,
                     glossary: Glossary, reviewed: bool,
                     translation_file: str,
                     reviewer_rejections: list[str] | None = None) -> dict:
    """The only path from a model's output to the ledger. Every check here
    is mechanical; the model's opinion never appears as an input."""
    prep = prepare_block(source_path, block_index)
    b: Block = prep["block"]

    reasons: list[str] = []

    # 1. placeholder restoration must be exact
    try:
        restored = thaw(prep["frozen"], translated_template)
    except ValueError as e:
        raise CandidateRejected([f"protected spans: {e}"])

    # 2. every protected span byte-identical in the final text
    missing = verify_protected_spans(b.raw, restored)
    if missing:
        reasons.append(f"spans missing after restore: {missing}")

    # 3. binding glossary terms honored
    reasons.extend(glossary.violations(b.raw, restored, lang))

    # 4. an unresolved reviewer rejection blocks promotion
    if reviewer_rejections:
        reasons.append(f"reviewer rejections open: {reviewer_rejections}")

    if reasons:
        raise CandidateRejected(reasons)

    # 5. place the translated block at the slot the ledger says it owns —
    #    never at the source's own index, which an adopted translation does
    #    not share (see _place)
    doc = Path(source_path).as_posix()
    raws = _read_raws(translation_file)
    mapping = block_mapping(ledger, doc, lang)
    target, shifted = _place(raws, mapping, block_index, restored)
    _write_raws(translation_file, raws)

    # capture time ONCE; the stored value is the hashed value
    seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()
    entry = ledger.append(
        op="translate",
        doc=doc,
        lang=lang,
        block_index=block_index,
        source_hash=b.source_hash,
        translation_hash=seal_text(restored),
        seq_time=seq_time,
        meta={"reviewed": reviewed, "translation_block_index": target},
    )
    _record_shift(ledger, doc, lang, shifted, seq_time)
    return {"entry": entry, "text": restored}


# ---------- reviewer tool ----------

def record_rejection(reason: str, findings: list[str]) -> dict:
    """The reviewer can only produce this. There is no approve()."""
    return {"verdict": "REJECT", "reason": reason, "findings": findings}
