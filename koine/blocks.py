"""Markdown → translation blocks, with protected spans frozen.

A *block* is the unit of translation and of sealing: a heading, a paragraph,
a list, a table row group, or a code fence. Code fences are blocks that are
never translated at all. Inside translatable blocks, *protected spans*
(inline code, URLs, env vars, CLI flags, file paths) are replaced by opaque
placeholders before any model sees the text, and must be restored
byte-identically afterwards — this is checked mechanically, and a candidate
translation that fails restoration is rejected without any model opinion.

Restoration is only half of it. Freezing is one-directional: it stops a model
from losing or altering a span the source had, and on its own says nothing
about spans the model *invents*. `invented_spans` is the other half.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .canonical import seal_text

PLACEHOLDER = "\u27e6K{n}\u27e7"  # ⟦K0⟧ ⟦K1⟧ … — unlikely to appear in prose
_PLACEHOLDER_RE = re.compile(r"\u27e6K(\d+)\u27e7")

_INLINE_CODE = re.compile(r"`[^`\n]+`")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")            # images & badges, whole
_URL = re.compile(r"https?://[^\s)\]>]*[^\s)\]>.,;:!?]")     # sans trailing punct.
_ANCHOR = re.compile(r"\(#[^)\s]+\)")                        # relative (#section)
_HTML_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_ENV_VAR = re.compile(r"(?<![\w.])[A-Z][A-Z0-9_]{2,}(?:=[^\s]+)?")
_CLI_FLAG = re.compile(r"(?<!\w)--?[A-Za-z][\w-]+")
_FILE_PATH = re.compile(r"(?<![\w/])(?:\.{0,2}/)[\w./-]+")

# Order matters: earlier patterns win. Each pattern captures a span that a
# translator must not touch.
_PROTECTED_PATTERNS = [_INLINE_CODE, _IMAGE, _URL, _ANCHOR, _HTML_TAG,
                       _ENV_VAR, _CLI_FLAG, _FILE_PATH]

# Span classes a translator has no reason to *introduce*. ENV_VARS/CONSTANTS
# and CLI flags are deliberately absent: ordinary prose in many target
# languages carries all-caps words (NOTA, ВНИМАНИЕ) and hyphenated compounds,
# and rejecting an honest translation over one of those would be worse than
# missing an invented flag -- which the source diff still shows a reviewer.
_UNINVENTABLE_PATTERNS = [_INLINE_CODE, _IMAGE, _URL, _HTML_TAG, _FILE_PATH]

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

    # Frontmatter: only at byte 0 of the document, and only when it closes
    # before the first blank line.
    #
    # Scanning for the closing delimiter anywhere in the document made a
    # one-line deletion catastrophic: drop the closing `---` of a real
    # frontmatter block and the next thematic break in the body closes it
    # instead, swallowing arbitrary prose into a block that is never
    # translated and, until now, never checked. Every generator that reads
    # frontmatter (Jekyll, Hugo, Docusaurus, MkDocs) treats it as one
    # contiguous document at the top of the file, so a blank line ends the
    # search. This fails visibly: frontmatter koine does not recognize becomes
    # ordinary translatable text, which shows up in `koine status`, instead of
    # prose that silently ships untranslated while the gate says all current.
    #
    # A UTF-8 BOM is tolerated for the delimiter test only -- it stays inside
    # the block's raw text, which is what gets hashed and mirrored.
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() == "":
                break
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


def _scan(text: str, patterns: list) -> list[tuple[int, int]]:
    """Non-overlapping (start, end) spans, one left-to-right pass over *text*.

    Earliest start wins; on a tie the longer span wins; on a full tie the
    earlier pattern keeps it. Shared so that "what counts as a protected span"
    has exactly one definition for both freezing and injection detection.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    while pos < len(text):
        best: tuple[int, int] | None = None
        for pattern in patterns:
            m = pattern.search(text, pos)
            if m is None:
                continue
            if best is None or m.start() < best[0] or (
                    m.start() == best[0] and m.end() > best[1]):
                best = (m.start(), m.end())
        if best is None:
            break
        spans.append(best)
        pos = best[1]
    return spans


def freeze(text: str) -> FrozenText:
    """Replace protected spans with placeholders. Deterministic, no model.

    One left-to-right scan over the *original* text, taking the earliest match
    among all patterns and, on a tie, the longest. Applying the patterns one
    after another over already-substituted text instead would let a later
    pattern swallow an earlier placeholder — `<a href="https://x">` freezes the
    URL first, then the HTML-tag pattern eats the whole tag including the
    placeholder, and the template can never be thawed. Scanning once cannot
    produce overlapping spans, and numbers the placeholders in reading order.
    """
    spans: list[str] = []
    out: list[str] = []
    pos = 0
    for start, end in _scan(text, _PROTECTED_PATTERNS):
        out.append(text[pos:start])
        spans.append(text[start:end])
        out.append(PLACEHOLDER.format(n=len(spans) - 1))
        pos = end
    out.append(text[pos:])
    return FrozenText(template="".join(out), spans=spans)


def invented_spans(translated_template: str) -> list[str]:
    """Protected-span constructs the model wrote itself, in its own template.

    freeze/thaw is one-directional. It guarantees no source span is lost or
    altered and says nothing about what else the model writes, so a translator
    that appends ``curl … | sudo sh`` invented a span the source never had and
    every mechanical check passed it through to CURRENT.

    Run on the *template*, not on the restored text: there every span that
    came from the source is still a placeholder, so nothing inherited can be
    mistaken for an invention. Only the classes in _UNINVENTABLE_PATTERNS are
    reported -- see the note there on what is deliberately left out.
    """
    return [translated_template[a:b]
            for a, b in _scan(translated_template, _UNINVENTABLE_PATTERNS)]


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
