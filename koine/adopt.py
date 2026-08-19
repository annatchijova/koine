"""`koine adopt` — bring a repo with pre-existing translations under koine
without lying about what is known.

Nobody starts from zero. Existing translations were made by hand, against
an unknown version of the source, possibly months stale. Adoption records
what can be established mechanically and nothing more:

- Blocks are aligned between source and translation by their *kind
  sequence* (heading/text/fence/frontmatter) using difflib. Alignment is a
  hypothesis, not a truth: every adopted pair is recorded with
  meta={"legacy": True} and surfaces as LEGACY_UNVERIFIED — visibly
  distinct from CURRENT, because nobody has verified the meaning matches.
- Unaligned source blocks stay UNTRANSLATED. Unaligned translation blocks
  are reported as orphans (content in the translation with no source
  counterpart — often deleted sections that were never removed) and each is
  recorded in the ledger *by content hash*. That recording is what lets the
  gate later tell a pre-existing orphan (known at adoption, allowed) from
  content appended to the translation afterwards (UNSOURCED, blocked). Hash
  identity, not index identity: an orphan that moves is still the same
  orphan, and an orphan that is edited is no longer the one that was adopted.
- From adoption onward, any change flows through the normal pipeline: a
  source edit turns the pair STALE; re-translating through the pipeline
  (or a human marking a pair reviewed) upgrades it out of legacy.

No model is involved anywhere in adoption. A model could *propose* better
alignments for messy cases; those proposals would still land as
legacy-unverified pairs. Mechanical alignment first, opinions never.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from .blocks import split_blocks
from .canonical import seal_text
from .store import Store


@dataclass
class AdoptReport:
    doc: str
    lang: str
    adopted: int
    untranslated: int
    orphans: list[int]


def align_by_kind(src_kinds: list[str], tr_kinds: list[str]) -> list[tuple[int, int]]:
    """Pair block indices whose kind sequences match. Deterministic."""
    sm = SequenceMatcher(a=src_kinds, b=tr_kinds, autojunk=False)
    pairs: list[tuple[int, int]] = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == "equal":
            pairs.extend((a0 + k, b0 + k) for k in range(a1 - a0))
    return pairs


def adopt(source_path: str | Path, translation_path: str | Path, lang: str,
          store: Store) -> AdoptReport:
    source_path = Path(source_path)
    translation_path = Path(translation_path)
    doc = source_path.as_posix()

    src_blocks = split_blocks(source_path.read_text(encoding="utf-8"))
    tr_blocks = split_blocks(translation_path.read_text(encoding="utf-8"))

    pairs = align_by_kind([b.kind for b in src_blocks],
                          [b.kind for b in tr_blocks])
    paired_src = {i for i, _ in pairs}
    paired_tr = {j for _, j in pairs}

    ledger = store.ledger(lang)
    seq_time = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for i, j in pairs:
        b = src_blocks[i]
        if b.kind in ("fence", "frontmatter"):
            # Never translated, so there is no meaning to adopt — but the
            # alignment itself is a placement fact, and dropping it would
            # leave the translation's fence mapped from nothing and reported
            # as UNSOURCED content. The hash recorded is the translation's
            # own: an adopted fence may legitimately differ from the source.
            ledger.append(
                op="mirror",
                doc=doc,
                lang=lang,
                block_index=i,
                source_hash=b.source_hash,
                translation_hash=seal_text(tr_blocks[j].raw),
                seq_time=seq_time,
                meta={"kind": b.kind, "legacy": True,
                      "translation_block_index": j},
            )
            continue
        ledger.append(
            op="translate",
            doc=doc,
            lang=lang,
            block_index=i,
            source_hash=b.source_hash,
            translation_hash=seal_text(tr_blocks[j].raw),
            seq_time=seq_time,
            meta={"legacy": True, "reviewed": False,
                  "translation_block_index": j},
        )

    orphans = [j for j in range(len(tr_blocks)) if j not in paired_tr]
    for j in orphans:
        ledger.append(
            op="adopt_orphan",
            doc=doc,
            lang=lang,
            block_index=j,          # translation-side index, informational only
            source_hash="",         # an orphan has no source block, by definition
            translation_hash=seal_text(tr_blocks[j].raw),
            seq_time=seq_time,
            meta={"kind": tr_blocks[j].kind, "side": "translation"},
        )

    return AdoptReport(
        doc=doc,
        lang=lang,
        adopted=sum(1 for i, _ in pairs
                    if src_blocks[i].kind not in ("fence", "frontmatter")),
        untranslated=len([b for k, b in enumerate(src_blocks)
                          if k not in paired_src]),
        orphans=orphans,
    )
