"""Canonical serialization and sealing.

Single source of truth for turning a payload into bytes and a SHA-256 seal.
Typed (bool checked before int), recursively ordered, versioned. No floats
are accepted anywhere in a sealed payload: floats belong to the narrative
layer, never to the decision layer.
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction

CANONICALIZE_VERSION = 1

_ALLOWED_SCALARS = (str, int, bool)


class FloatInSealedPayload(TypeError):
    """A float reached the decision path. That is a defect, not a warning."""


def canonicalize(obj):
    """Return a JSON-safe structure with explicit types, recursively ordered."""
    if isinstance(obj, bool):  # before int: bool subclasses int
        return {"t": "bool", "v": obj}
    if isinstance(obj, int):
        return {"t": "int", "v": str(obj)}
    if isinstance(obj, float):
        raise FloatInSealedPayload(
            "float in sealed payload; use Fraction, int, or str"
        )
    if isinstance(obj, str):
        return {"t": "str", "v": obj}
    if isinstance(obj, Fraction):
        return {"t": "frac", "v": f"{obj.numerator}/{obj.denominator}"}
    if obj is None:
        return {"t": "null"}
    if isinstance(obj, (list, tuple)):
        return {"t": "list", "v": [canonicalize(x) for x in obj]}
    if isinstance(obj, dict):
        items = []
        for k in sorted(obj.keys()):
            if not isinstance(k, str):
                raise TypeError(f"non-str dict key in sealed payload: {k!r}")
            items.append([k, canonicalize(obj[k])])
        return {"t": "dict", "v": items}
    if isinstance(obj, (set, frozenset)):
        return {"t": "set", "v": sorted(canonicalize(x)["v"] if isinstance(x, str) else json.dumps(canonicalize(x), sort_keys=True) for x in obj)}
    raise TypeError(f"unsupported type in sealed payload: {type(obj).__name__}")


def canonical_bytes(payload) -> bytes:
    envelope = {"canonicalize_version": CANONICALIZE_VERSION, "payload": canonicalize(payload)}
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal(payload) -> str:
    """SHA-256 hex digest over the canonical bytes of *payload*."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def seal_text(text: str) -> str:
    """Seal a plain string (used for source/translation block content)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
