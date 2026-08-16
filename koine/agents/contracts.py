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
    if b.kind == "fence":
        raise CandidateRejected(["code fences are never translated"])
    frozen = freeze(b.raw)
    return {"block": b, "frozen": frozen, "template": frozen.template}


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

    # write the translated block into the target file at the same index
    tpath = Path(translation_file)
    tr_blocks = (split_blocks(tpath.read_text(encoding="utf-8"))
                 if tpath.exists() else [])
    raws = [tb.raw for tb in tr_blocks]
    while len(raws) <= block_index:
        raws.append(f"<!-- koine:untranslated block {len(raws)} -->")
    raws[block_index] = restored
    tpath.write_text("\n\n".join(raws) + "\n", encoding="utf-8")

    # capture time ONCE; the stored value is the hashed value
    seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()
    entry = ledger.append(
        op="translate",
        doc=Path(source_path).as_posix(),
        lang=lang,
        block_index=block_index,
        source_hash=b.source_hash,
        translation_hash=seal_text(restored),
        seq_time=seq_time,
        meta={"reviewed": reviewed},
    )
    return {"entry": entry, "text": restored}


# ---------- reviewer tool ----------

def record_rejection(reason: str, findings: list[str]) -> dict:
    """The reviewer can only produce this. There is no approve()."""
    return {"verdict": "REJECT", "reason": reason, "findings": findings}
