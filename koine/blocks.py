"""Markdown → translation blocks, with protected spans frozen.

A *block* is the unit of translation and of sealing: a heading, a paragraph,
a list, a table row group, or a code fence. Code fences are blocks that are
never translated at all. Inside translatable blocks, *protected spans*
(inline code, URLs, env vars, CLI flags, file paths) are replaced by opaque
placeholders before any model sees the text, and must be restored
byte-identically afterwards — this is checked mechanically, and a candidate
translation that fails restoration is rejected without any model opinion.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .canonical import seal_text

PLACEHOLDER = "\u27e6K{n}\u27e7"  # ⟦K0⟧ ⟦K1⟧ … — unlikely to appear in prose
_PLACEHOLDER_RE = re.compile(r"\u27e6K(\d+)\u27e7")

# Order matters: earlier patterns win. Each pattern captures a span that a
# translator must not touch.
_PROTECTED_PATTERNS = [
    re.compile(r"`[^`\n]+`"),                          # inline code
    re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)"),           # images & badges, whole
    re.compile(r"https?://[^\s)\]>]*[^\s)\]>.,;:!?]"), # URLs (sans trailing punct.)
    re.compile(r"\(#[^)\s]+\)"),                       # relative anchors (#section)
    re.compile(r"</?[A-Za-z][^>\n]*>"),                # inline HTML tags
    re.compile(r"(?<![\w.])[A-Z][A-Z0-9_]{2,}(?:=[^\s]+)?"),  # ENV_VARS / CONSTANTS
    re.compile(r"(?<!\w)--?[A-Za-z][\w-]+"),           # CLI flags
    re.compile(r"(?<![\w/])(?:\.{0,2}/)[\w./-]+"),     # file paths
]

_FENCE_RE = re.compile(r"^(```|~~~)")


@dataclass
class Block:
    index: int
    kind: str                 # "fence" | "text"
    raw: str                  # exact source text of the block
    source_hash: str = ""

    def __post_init__(self):
        if not self.source_hash:
            self.source_hash = seal_text(self.raw)


@dataclass
class FrozenText:
    """A translatable block with protected spans swapped for placeholders."""
    template: str
    spans: list[str] = field(default_factory=list)


def split_blocks(markdown: str) -> list[Block]:
    """Split a markdown document into blocks. Code fences are single blocks.

    YAML frontmatter (--- ... --- at the very top, as used by MkDocs,
    Docusaurus, Jekyll, Hugo) becomes a single "frontmatter" block that is
    never translated — keys and config values are machine-facing.
    """
    lines = markdown.split("\n")
    blocks: list[Block] = []
    buf: list[str] = []
    in_fence = False
    fence_marker = ""
    start = 0

    # frontmatter: only at byte 0 of the document
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                blocks.append(Block(index=0, kind="frontmatter",
                                    raw="\n".join(lines[: j + 1])))
                start = j + 1
                break

    def flush(kind: str):
        nonlocal buf
        if buf:
            blocks.append(Block(index=len(blocks), kind=kind, raw="\n".join(buf)))
            buf = []

    for line in lines[start:]:
        m = _FENCE_RE.match(line)
        if in_fence:
            buf.append(line)
            if m and line.strip().startswith(fence_marker):
                in_fence = False
                flush("fence")
        elif m:
            flush("text")
            in_fence = True
            fence_marker = m.group(1)
            buf.append(line)
        elif line.strip() == "":
            flush("text")
        else:
            buf.append(line)
    flush("fence" if in_fence else "text")
    return blocks


def freeze(text: str) -> FrozenText:
    """Replace protected spans with placeholders. Deterministic, no model."""
    spans: list[str] = []
    out = text
    for pattern in _PROTECTED_PATTERNS:
        def _sub(m, _spans=spans):
            _spans.append(m.group(0))
            return PLACEHOLDER.format(n=len(_spans) - 1)
        out = pattern.sub(_sub, out)
    return FrozenText(template=out, spans=spans)


def thaw(frozen: FrozenText, translated_template: str) -> str:
    """Restore protected spans into a translated template.

    Raises ValueError if any placeholder is missing, duplicated, or unknown —
    that is a mechanical rejection of the candidate translation.
    """
    seen = _PLACEHOLDER_RE.findall(translated_template)
    expected = [str(i) for i in range(len(frozen.spans))]
    if sorted(seen) != sorted(expected):
        raise ValueError(
            f"placeholder mismatch: expected {expected}, found {sorted(seen)}"
        )

    def _restore(m):
        return frozen.spans[int(m.group(1))]

    return _PLACEHOLDER_RE.sub(_restore, translated_template)


def verify_protected_spans(source: str, translation: str) -> list[str]:
    """Return the protected spans from *source* missing in *translation*.

    Mechanical post-check independent of freeze/thaw: every protected span
    that exists in the source must appear byte-identical in the translation.
    Empty list means the translation preserves every span.
    """
    missing = []
    for span in freeze(source).spans:
        if span not in translation:
            missing.append(span)
    return missing
