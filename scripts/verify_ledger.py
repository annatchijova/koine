#!/usr/bin/env python3
"""Standalone koine ledger verifier. Standard library only.

Deliberately imports NOTHING from koine: if the pipeline itself were
compromised, this verifier would still catch a rewritten ledger. It
re-derives the canonicalization and the chain from the spec alone.

It also checks the anchor sidecar (<ledger>.anchor.json) when present: a
truncated tail is a valid chain, so linkage and integrity alone cannot see
it. An absent anchor is reported, never assumed away.

Exit codes: 0 VERIFIED, 1 BROKEN, 2 NO_LEDGER.
"""
import hashlib
import json
import os
import sys

GENESIS = "0" * 64
CANONICALIZE_VERSION = 1
ANCHOR_VERSION = 1


def canonicalize(obj):
    if isinstance(obj, bool):
        return {"t": "bool", "v": obj}
    if isinstance(obj, int):
        return {"t": "int", "v": str(obj)}
    if isinstance(obj, float):
        raise SystemExit("float in sealed payload: ledger is invalid by construction")
    if isinstance(obj, str):
        return {"t": "str", "v": obj}
    if obj is None:
        return {"t": "null"}
    if isinstance(obj, list):
        return {"t": "list", "v": [canonicalize(x) for x in obj]}
    if isinstance(obj, dict):
        return {"t": "dict", "v": [[k, canonicalize(obj[k])] for k in sorted(obj)]}
    raise SystemExit(f"unsupported type: {type(obj).__name__}")


def canonical_bytes(payload):
    env = {"canonicalize_version": CANONICALIZE_VERSION, "payload": canonicalize(payload)}
    return json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def read_anchor(path):
    """(head, count) recorded for this chain, or None if there is no anchor."""
    anchor_path = os.path.splitext(path)[0] + ".anchor.json"
    try:
        data = json.loads(open(anchor_path, encoding="utf-8").read())
    except FileNotFoundError:
        return None
    if data.get("anchor_version") != ANCHOR_VERSION:
        raise SystemExit(f"anchor version {data.get('anchor_version')!r} not supported")
    return data["head"], data["count"]


def main(path):
    try:
        lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    except FileNotFoundError:
        print("NO_LEDGER")
        return 2
    entries = [json.loads(l) for l in lines]
    issues = []
    for i, e in enumerate(entries):
        h = hashlib.sha256(canonical_bytes(e["payload"]) + e["prev_hash"].encode()).hexdigest()
        if h != e["audit_hash"]:
            issues.append(f"seq {e.get('seq', i)}: integrity — audit_hash does not recompute")
    for a, b in zip(entries, entries[1:]):
        if b["prev_hash"] != a["audit_hash"]:
            issues.append(f"seq {b.get('seq')}: linkage — prev_hash mismatch")
    if entries and entries[0]["prev_hash"] != GENESIS:
        issues.append("linkage — first entry does not descend from genesis")

    anchor = read_anchor(path)
    if anchor is not None:
        head, count = anchor
        if count > len(entries):
            issues.append(f"truncation — {len(entries)} entries present, "
                          f"{count} recorded in the anchor")
        elif count < len(entries):
            issues.append(f"anchor lag — {len(entries)} entries present, "
                          f"{count} recorded in the anchor")
        elif entries and entries[-1]["audit_hash"] != head:
            issues.append("truncation — chain head does not match the anchor")

    if issues:
        print("BROKEN")
        for s in issues:
            print(f"  - {s}")
        return 1
    if anchor is None:
        # intact, but say what was not checked rather than implying it was
        print(f"VERIFIED ({len(entries)} entries; no anchor — "
              f"a truncated tail would not be detected)")
        return 0
    print(f"VERIFIED ({len(entries)} entries, anchor matches)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: verify_ledger.py <ledger.jsonl>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
