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
import os
from pathlib import Path

from ..blocks import (Block, as_single_block, freeze, invented_spans,
                      split_blocks, thaw, verify_protected_spans)
from ..canonical import seal_text
from ..glossary import Glossary
from ..ledger import Ledger
from ..state import (NEVER_TRANSLATED_KINDS, adopted_orphan_hashes,
                     block_mapping, latest_events,
                     recorded_translation_hashes)


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
    """Write the translated file so that reading it back yields exactly *raws*.

    Two properties the previous one-liner did not have. First it is checked:
    a raw that does not survive the round trip (one carrying a blank line, an
    unterminated fence) would renumber every block after it and desynchronize
    the ledger from the file, so it is refused before anything is written
    rather than discovered later as TAMPERED. Second it is atomic: a partial
    write left a truncated translation whose every block read as tampered,
    and the ledger next door already replaces its anchor with tmp+os.replace.
    """
    text = "\n\n".join(raws) + "\n"
    if [b.raw for b in split_blocks(text)] != list(raws):
        raise CandidateRejected([
            "writing these blocks would not read back as the same blocks; "
            "refusing to desynchronize the translated file from the ledger"
        ])
    path = Path(translation_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
    with ledger.transaction():
        doc = Path(source_path).as_posix()
        src_blocks = split_blocks(Path(source_path).read_text(encoding="utf-8"))
        raws = _read_raws(translation_file)
        mapping = block_mapping(ledger, doc, lang)
        seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()

        events = latest_events(ledger)
        mirrored: list[int] = []
        for b in src_blocks:
            if b.kind not in NEVER_TRANSLATED_KINDS:
                continue
            target = mapping.get(b.index)
            ev = events.get((doc, lang, b.index))
            if (target is not None and target < len(raws) and ev is not None
                    and ev["payload"]["source_hash"] == b.source_hash):
                # placed, and the source has not changed since. Decided from the
                # ledger, not from comparing bytes: an adopted fence may differ
                # from the source (a localized comment), and re-copying over it
                # because the bytes differ would silently discard that.
                continue
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


def retire_orphaned(*, source_path: str, translation_file: str, lang: str,
                    ledger: Ledger) -> list[int]:
    """Remove translations whose source block was deleted, and vacate the
    placements those blocks left behind.

    Without this the pipeline can only add and overwrite: delete a section from
    the source and the translated file keeps documenting it forever — the exact
    "quietly out of date" failure koine exists to prevent, and one the gate
    reports as UNSOURCED with no way to fix it through the tool.

    Deletion is the change with the least visible consequences, so removing
    file content is fenced in: a block goes only if its content hash is one
    koine itself recorded writing for this pair. Content koine did not write —
    a hand-added paragraph, an adopted orphan — is never touched; it stays
    visible as UNSOURCED or LEGACY_ORPHAN for a human to resolve. Every removal
    is recorded as a `retire` event carrying the removed content's hash, so
    what was dropped stays auditable.

    Vacating matters as much as removing. A placement left behind by a document
    that used to be longer keeps shadowing whatever new block later lands on
    that index: the newcomer reads as STALE against a stranger's hash, and its
    recorded slot points past the end of the translated file, so it can never
    be placed and never stops being pending.
    """
    with ledger.transaction():
        doc = Path(source_path).as_posix()
        src_blocks = split_blocks(Path(source_path).read_text(encoding="utf-8"))
        raws = _read_raws(translation_file)
        mapping = block_mapping(ledger, doc, lang)
        written = recorded_translation_hashes(ledger, doc, lang)
        keep = adopted_orphan_hashes(ledger, doc, lang)

        # a placement for a source index the document no longer has belongs to a
        # deleted block: its slot is stranded, not occupied
        dead = sorted(i for i in mapping if i >= len(src_blocks))
        occupied = {tgt for i, tgt in mapping.items() if i < len(src_blocks)}
        doomed = [j for j, raw in enumerate(raws)
                  if j not in occupied
                  and seal_text(raw) not in keep
                  and seal_text(raw) in written]
        if not doomed and not dead:
            return []

        seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()

        for i in dead:
            ledger.append(
                op="retire", doc=doc, lang=lang, block_index=i,
                source_hash="", translation_hash="",
                seq_time=seq_time,
                meta={"reason": "source block deleted", "side": "source"},
            )

        for j in sorted(doomed, reverse=True):
            removed = raws.pop(j)
            ledger.append(
                op="retire", doc=doc, lang=lang, block_index=j,
                source_hash="",                      # its source block is gone
                translation_hash=seal_text(removed),
                seq_time=seq_time,
                meta={"reason": "source block deleted", "side": "translation"},
            )
            shifted = {i: tgt - 1 for i, tgt in mapping.items()
                       if tgt > j and i < len(src_blocks)}
            _record_shift(ledger, doc, lang, shifted, seq_time)
            mapping.update(shifted)

        if doomed:
            _write_raws(translation_file, raws)
    return sorted(doomed)


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

    # 2. the model may not invent protected spans of its own. thaw guarantees
    #    only that nothing from the source was lost or altered; it says
    #    nothing about what else the model wrote, so a translator that
    #    appended its own `curl … | sudo sh` cleared every check below and was
    #    promoted to CURRENT. Checked on the template, where anything that
    #    came from the source is still a placeholder.
    invented = invented_spans(translated_template)
    if invented:
        reasons.append(f"protected spans the source never had: {invented}")

    # 3. redundant assertion that every source span survived restoration.
    #    thaw already guarantees this — it rejects any placeholder multiset
    #    that is not exactly the expected one and then substitutes all of
    #    them — so this can fire only if thaw itself regresses. Kept as a
    #    cheap tripwire on that, not as an independent guarantee: it is not
    #    one, and describing it as one overstated what is verified.
    missing = verify_protected_spans(b.raw, restored)
    if missing:
        reasons.append(f"spans missing after restore: {missing}")

    # 4. the candidate must be exactly one block, or sealing it seals
    #    something the file will never read back — see as_single_block
    single = as_single_block(restored)
    if single is None:
        reasons.append(
            f"a translated block must read back as one text block; this "
            f"candidate is {len(split_blocks(restored))}")
    else:
        restored = single.raw

    # 5. binding glossary terms honored
    reasons.extend(glossary.violations(b.raw, restored, lang))

    # 6. an unresolved reviewer rejection blocks promotion
    if reviewer_rejections:
        reasons.append(f"reviewer rejections open: {reviewer_rejections}")

    if reasons:
        raise CandidateRejected(reasons)

    # 7. place the translated block at the slot the ledger says it owns —
    #    never at the source's own index, which an adopted translation does
    #    not share (see _place)
    doc = Path(source_path).as_posix()
    with ledger.transaction():
        # read, place, write and append are one fact. Split across the lock
        # boundary, two writers each read the same file, each placed a block,
        # and the second write erased the first while both entries landed.
        #
        # The lock removes the concurrent writer. It does not make the pair
        # crash-atomic, and the three writers do not even agree on which half
        # goes first: here the file is written per block before its entry,
        # while mirror_untranslatable and retire_orphaned append their entries
        # first and write the file once at the end. Both orders surface as
        # TAMPERED, so nothing is silently wrong either way — but they leave
        # different states behind for a human to recover from (content present
        # and unrecorded, versus recorded and absent), and picking one means
        # deciding what a half-finished operation should mean. That decision
        # is open; see docs/RED-TEAM.md, F7.
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
