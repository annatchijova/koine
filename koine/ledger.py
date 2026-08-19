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

Linkage and integrity cannot see a *truncated tail*: entries 0..7 of a valid
0..9 chain are themselves a perfectly valid chain. So each chain keeps an
`anchor` sidecar naming its expected head hash and length. Its honest limit:
it turns truncation into a two-file edit that shows up in review and in git
history, and it catches accidental loss (a bad merge, a partial write, one
file reverted). It does not stop someone who rewrites both files — only a
copy koine does not control, such as the remote git history, does that.
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
ANCHOR_VERSION = 1

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


class UnsupportedAnchor(Exception):
    pass


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
    @property
    def anchor_path(self) -> Path:
        """Sidecar naming this chain's expected head and length."""
        return self.path.with_suffix(".anchor.json")

    def entries(self) -> list[dict]:
        """Parsed entries. A line that is not valid JSON is a damaged chain,
        so it is surfaced as an unverifiable entry rather than a traceback
        from inside the parser — `verify` then reports it as an issue."""
        if not self.path.exists():
            return []
        out = []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                out.append({"_unparsable": f"line {n + 1}: {e}"})
        return out

    def read_anchor(self) -> dict | None:
        if not self.anchor_path.exists():
            return None
        data = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        if data.get("anchor_version") != ANCHOR_VERSION:
            raise UnsupportedAnchor(
                f"anchor version {data.get('anchor_version')!r} not supported "
                f"(expected {ANCHOR_VERSION}); migrate explicitly, do not guess"
            )
        return data

    def _write_anchor(self, head: str, count: int) -> None:
        """Replace the anchor atomically: a half-written anchor would report
        every intact chain as truncated."""
        tmp = self.anchor_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"anchor_version": ANCHOR_VERSION, "head": head, "count": count},
            sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.anchor_path)

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
            # inside the lock: an anchor that lags the chain reads as truncation
            self._write_anchor(entry["audit_hash"], len(es) + 1)
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
        if "_unparsable" in e:
            integrity_ok = False
            issues.append(f"entry {i}: not valid JSON — {e['_unparsable']}")
            continue
        recomputed = _entry_hash(e["payload"], e["prev_hash"])
        if recomputed != e["audit_hash"]:
            integrity_ok = False
            issues.append(f"seq {e.get('seq', i)}: audit_hash does not recompute")

    for a, b in zip(entries, entries[1:]):
        if "_unparsable" in a or "_unparsable" in b:
            continue  # already reported; linkage across a hole is meaningless
        if b["prev_hash"] != a["audit_hash"]:
            linkage_ok = False
            issues.append(f"seq {b.get('seq')}: prev_hash does not match previous entry")

    if entries and not allow_partial and "_unparsable" not in entries[0] \
            and entries[0]["prev_hash"] != GENESIS:
        linkage_ok = False
        issues.append("first entry does not descend from genesis")

    return {"linkage_ok": linkage_ok, "integrity_ok": integrity_ok, "issues": issues}


def verify_chain(ledger: "Ledger") -> dict:
    """Everything `verify` checks, plus what it structurally cannot see.

    Linkage and integrity are properties of the entries that are present, so a
    truncated tail passes both: 0..7 of a 0..9 chain is a valid chain. Only an
    external record of the expected head and length catches that, which is
    what the anchor sidecar is for.

    `anchored` is reported separately from `ok` on purpose: a chain with no
    anchor is not broken, but the truncation guarantee does not hold for it,
    and saying "VERIFIED" without saying that would be the kind of confident
    half-truth koine exists to prevent.
    """
    entries = ledger.entries()
    report = verify(entries)
    report["entries"] = len(entries)
    report["truncated"] = False
    report["anchor_lag"] = 0

    try:
        anchor = ledger.read_anchor()
    except UnsupportedAnchor as e:
        anchor = None
        report["issues"].append(str(e))
        report["integrity_ok"] = False

    if anchor is None:
        report["anchored"] = False
        report["ok"] = report["linkage_ok"] and report["integrity_ok"]
        return report

    report["anchored"] = True
    if anchor["count"] > len(entries):
        report["truncated"] = True
        missing = anchor["count"] - len(entries)
        report["issues"].append(
            f"chain is shorter than its anchor: {len(entries)} entries present, "
            f"{anchor['count']} recorded — {missing} "
            f"{'entry was' if missing == 1 else 'entries were'} "
            f"removed from the tail")
    elif anchor["count"] < len(entries):
        # a crash between the entry write and the anchor write, or an append
        # that bypassed koine. Nothing is missing, so this is not truncation;
        # the next append re-anchors. Reported, never silently absorbed.
        report["anchor_lag"] = len(entries) - anchor["count"]
        report["issues"].append(
            f"chain is longer than its anchor: {len(entries)} entries present, "
            f"{anchor['count']} recorded — appended without updating the anchor")
    elif entries and entries[-1].get("audit_hash") != anchor["head"]:
        report["truncated"] = True
        report["issues"].append(
            "chain head does not match the anchor — the tail was replaced")

    report["ok"] = (report["linkage_ok"] and report["integrity_ok"]
                    and not report["truncated"])
    return report
