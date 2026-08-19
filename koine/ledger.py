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

import contextlib
import hashlib
import json
import os
from pathlib import Path

from .canonical import canonical_bytes

GENESIS = "0" * 64
TAIL_CHECK_DEPTH = 16

try:  # POSIX advisory locking; absent on Windows
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None


class BrokenTail(Warning):
    pass


class Unlockable(Exception):
    """The chain could not be locked for writing, so it was not written.

    Appending is read-tail-then-write. Without a lock, two concurrent writers
    read the same tail and emit two entries claiming the same `prev_hash` —
    a forked chain that `verify` reports as broken linkage after the fact.
    Refusing to write beats forking the chain.
    """


@contextlib.contextmanager
def _exclusive(path: Path):
    """Hold an exclusive lock on a sidecar of *path* for the whole append."""
    if fcntl is None:  # pragma: no cover - platform dependent
        raise Unlockable(
            "file locking is unavailable on this platform; koine will not "
            "append without it — run appends from a single process"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


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

        The read-tail-and-write is held under an exclusive lock: per-language
        chains exist so concurrent PRs do not conflict, which means concurrent
        writers to one chain are the expected case, not the exotic one.
        """
        with _exclusive(self.path):
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
                f.flush()
                os.fsync(f.fileno())
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
