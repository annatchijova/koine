"""Hash-chained, append-only ledger of translation events.

Lives in the repo as a JSONL file (one entry per line) so it travels with
git, diffs cleanly, and can be verified by anyone with the standard library.

Each entry seals: the operation, the document path, the language, the block
index, the SOURCE block hash the translation was made against, and the
TRANSLATION content hash — content hashes, not just identifiers, so editing
a translated file after the fact breaks recomputation (failure mode #1 of
tamper-evident chains).

audit_hash = sha256(canonical(payload) + prev_hash). One genesis, ever.
Appending onto a broken tail is recorded but loudly flagged, never laundered.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .canonical import canonical_bytes

GENESIS = "0" * 64
TAIL_CHECK_DEPTH = 16


class BrokenTail(Warning):
    pass


def _entry_hash(payload: dict, prev_hash: str) -> str:
    return hashlib.sha256(canonical_bytes(payload) + prev_hash.encode("ascii")).hexdigest()


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---------- read ----------
    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def last_hash(self) -> str:
        es = self.entries()
        return es[-1]["audit_hash"] if es else GENESIS

    # ---------- write ----------
    def append(self, *, op: str, doc: str, lang: str, block_index: int,
               source_hash: str, translation_hash: str, seq_time: str,
               meta: dict | None = None) -> dict:
        """Append one event. *seq_time* is captured once by the caller and is
        the same value stored and hashed (failure mode #2). Returns the entry.
        """
        tail_warning = None
        es = self.entries()
        if es:
            tail = es[-TAIL_CHECK_DEPTH:]
            report = verify(tail, allow_partial=True)
            if not (report["linkage_ok"] and report["integrity_ok"]):
                tail_warning = f"appending onto a broken tail: {report['issues']}"

        payload = {
            "op": op,
            "doc": doc,
            "lang": lang,
            "block_index": block_index,
            "source_hash": source_hash,
            "translation_hash": translation_hash,
            "seq_time": seq_time,
            "meta": meta or {},
        }
        prev = es[-1]["audit_hash"] if es else GENESIS
        entry = {
            "seq": len(es),
            "payload": payload,
            "prev_hash": prev,
            "audit_hash": _entry_hash(payload, prev),
        }
        if tail_warning:
            entry["tail_warning"] = tail_warning
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")
        return entry


def verify(entries: list[dict], allow_partial: bool = False) -> dict:
    """Verify linkage and integrity as two independent properties.

    A chain can be linked but edited, or intact but reordered; collapsing
    the two into one boolean hides which attack happened.
    """
    issues: list[str] = []
    linkage_ok = True
    integrity_ok = True

    for i, e in enumerate(entries):
        recomputed = _entry_hash(e["payload"], e["prev_hash"])
        if recomputed != e["audit_hash"]:
            integrity_ok = False
            issues.append(f"seq {e.get('seq', i)}: audit_hash does not recompute")

    for a, b in zip(entries, entries[1:]):
        if b["prev_hash"] != a["audit_hash"]:
            linkage_ok = False
            issues.append(f"seq {b.get('seq')}: prev_hash does not match previous entry")

    if entries and not allow_partial and entries[0]["prev_hash"] != GENESIS:
        linkage_ok = False
        issues.append("first entry does not descend from genesis")

    return {"linkage_ok": linkage_ok, "integrity_ok": integrity_ok, "issues": issues}
