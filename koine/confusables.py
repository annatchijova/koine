"""Where visible equality, byte equality and hash equality come apart.

koine derives every decision from byte-level hashes, which is what makes it
mechanical — and is exactly the seam an attacker aims at. Three strings that
render identically on screen can hash three different ways, so text can be
engineered to look like one thing to the human reviewing the diff and be
another thing to the machine deriving the state.

This module does not normalize that away. Normalizing would destroy the
property the ledger depends on: that a recorded hash means one exact sequence
of bytes. It *reports* the constructs instead, so a difference a reader cannot
see is never a difference only koine can see.

Two detections, both deliberately narrow — a false positive here means
rejecting honest documentation:

- `invisible_controls`: characters that render as nothing and are not needed
  to render anything. ZWJ/ZWNJ are excluded (they are load-bearing in Indic,
  Arabic and Persian text and in emoji sequences) and so is the non-breaking
  space (ordinary French and Spanish typography).
- `mixed_script_tokens`: one word built from more than one writing system.
  Whole-word Cyrillic, Greek, Japanese or Korean is ordinary text and is not
  reported; `paypаl` with one Cyrillic а is not ordinary anything.

`skeleton` folds the well-known lookalikes to ASCII, for asking whether a
term is present *in disguise*. It is a curated table, not the full Unicode
confusables data: it answers "is this a disguised ASCII word", not "are these
two arbitrary strings confusable".
"""
from __future__ import annotations

import re
import unicodedata

# Renders as nothing, needed to render nothing. U+200D/U+200C (ZWJ/ZWNJ) and
# U+00A0 (NBSP) are deliberately absent — see the module docstring.
INVISIBLE = {
    "​": "ZERO WIDTH SPACE",
    "⁠": "WORD JOINER",
    "­": "SOFT HYPHEN",
    "᠎": "MONGOLIAN VOWEL SEPARATOR",
    "‪": "LEFT-TO-RIGHT EMBEDDING",
    "‫": "RIGHT-TO-LEFT EMBEDDING",
    "‬": "POP DIRECTIONAL FORMATTING",
    "‭": "LEFT-TO-RIGHT OVERRIDE",
    "‮": "RIGHT-TO-LEFT OVERRIDE",
    "⁦": "LEFT-TO-RIGHT ISOLATE",
    "⁧": "RIGHT-TO-LEFT ISOLATE",
    "⁨": "FIRST STRONG ISOLATE",
    "⁩": "POP DIRECTIONAL ISOLATE",
}

# Curated lookalikes of ASCII letters. Cyrillic and Greek carry the ones that
# actually get used for this; the fullwidth forms are folded by NFKC below.
_FOLD = {
    "а": "a", "в": "b", "с": "c", "е": "e", "һ": "h", "і": "i", "ј": "j",
    "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "ѕ": "s", "т": "t",
    "у": "y", "х": "x", "ԁ": "d", "ѵ": "v", "ԝ": "w",
    "А": "A", "В": "B", "С": "C", "Е": "E", "Н": "H", "І": "I", "Ј": "J",
    "К": "K", "М": "M", "О": "O", "Р": "P", "Ѕ": "S", "Т": "T", "У": "Y",
    "Х": "X",
    "α": "a", "ο": "o", "ρ": "p", "ν": "v", "τ": "t", "υ": "u", "χ": "x",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}

# Writing systems that legitimately share a word. Japanese mixes Han with both
# kana; Korean mixes Hangul with Han. Treating them as one script is what
# keeps this check from firing on every Japanese sentence.
_CJK = {"CJK", "HIRAGANA", "KATAKANA", "BOPOMOFO", "HANGUL", "IDEOGRAPHIC"}

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def nfc(text: str) -> str:
    """Canonical composition. Only for *comparison* — never write this back
    into a file koine has sealed, or every hash for it changes at once."""
    return unicodedata.normalize("NFC", text)


def _script_of(ch: str) -> str | None:
    """Coarse writing system of one character, or None if it carries none."""
    if not ch.isalpha():
        return None
    try:
        name = unicodedata.name(ch)
    except ValueError:                       # unnamed character
        return None
    head = name.split()[0]
    if head in _CJK or name.startswith("CJK"):
        return "CJK"
    return head


def invisible_controls(text: str) -> list[tuple[int, str, str]]:
    """(offset, character, Unicode name) for every zero-width or bidi control.

    A single one of these in a source block is enough to change its hash while
    leaving the rendered diff visually empty.
    """
    return [(i, ch, INVISIBLE[ch]) for i, ch in enumerate(text) if ch in INVISIBLE]


def mixed_script_tokens(text: str) -> list[str]:
    """Words built from more than one writing system, in order of appearance."""
    out = []
    for m in _TOKEN_RE.finditer(nfc(text)):
        token = m.group(0)
        scripts = {s for s in (_script_of(c) for c in token) if s}
        if len(scripts) > 1:
            out.append(token)
    return out


def skeleton(text: str) -> str:
    """Fold *text* toward the ASCII it is pretending to be.

    Three steps, each closing a different way to write a word that is not the
    word it renders as: compatibility normalization (fullwidth and other
    presentation forms), removal of the characters that render as nothing, and
    the curated lookalike table.

    Dropping the invisibles is what makes this answer the question honestly.
    Without it a zero-width space inside an ASCII word defeated both halves at
    once — the term was not found literally, and the word is entirely Latin so
    it is not mixed-script either — and the binding rule went silent with
    nothing said about why.

    The result is not length-preserving and is only ever compared as a whole
    against another skeleton; never align it positionally with the original.
    """
    folded = unicodedata.normalize("NFKC", nfc(text))
    stripped = "".join(ch for ch in folded if ch not in INVISIBLE)
    return "".join(_FOLD.get(ch, ch) for ch in stripped)


def findings(text: str, where: str) -> list[str]:
    """Human-readable report lines for one document. Empty when it is clean."""
    out: list[str] = []
    seen: set[str] = set()
    for _, ch, name in invisible_controls(text):
        if ch in seen:
            continue
        seen.add(ch)
        out.append(f"{where}: invisible character U+{ord(ch):04X} ({name}) — "
                   f"changes the hash without changing what a reader sees")
    for token in dict.fromkeys(mixed_script_tokens(text)):
        scripts = sorted({s for s in (_script_of(c) for c in token) if s})
        out.append(f"{where}: {token!r} mixes {' and '.join(scripts)} letters — "
                   f"a lookalike, not the word it renders as")
    return out
