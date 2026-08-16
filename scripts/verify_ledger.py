#!/usr/bin/env python3
"""Standalone koine ledger verifier. Standard library only.

Deliberately imports NOTHING from koine: if the pipeline itself were
compromised, this verifier would still catch a rewritten ledger. It
re-derives the canonicalization and the chain from the spec alone.

Exit codes: 0 VERIFIED, 1 BROKEN, 2 NO_LEDGER.
"""
import hashlib
import json
import sys

GENESIS = "0" * 64
CANONICALIZE_VERSION = 1


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
    if issues:
        print("BROKEN")
        for s in issues:
            print(f"  - {s}")
        return 1
    print(f"VERIFIED ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: verify_ledger.py <ledger.jsonl>")
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
