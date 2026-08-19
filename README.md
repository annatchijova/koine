# koine

**Multilingual repos that cannot silently lie.** koine keeps a repository's
documentation synchronized across languages — not by translating harder, but
by making it *mechanically impossible* for a stale or edited translation to
claim it is current. An agent fleet proposes translations, reviews them
adversarially, and curates terminology; **no model ever decides what counts
as up to date.** State is derived from hashes alone.

> The unlikely hero: the non-English-speaking developer. Most of the world's
> programmers read documentation in a language they didn't grow up in — and
> most translated docs are quietly out of date, which is worse than absent:
> a README that lies tells you the old pytest command with total confidence.

## The one invariant

A translation's state (`CURRENT`, `STALE`, `TAMPERED`, `MACHINE_ONLY`,
`UNTRANSLATED`) is a pure function of content hashes and a tamper-evident
ledger — including *where* each translated block lives, which is read from
the ledger and never inferred from the source's own block numbering (a
translation with a banner the source lacks is not index-aligned with it).
The fleet's language models:

- receive prose with protected spans (code, URLs, env vars, flags, paths)
  **frozen into placeholders** they cannot alter — restoration is verified
  byte-for-byte, mechanically;
- can **reject** a candidate translation with findings, but cannot approve
  one — there is no `approve()` tool, because a model's approval would put
  a model back into the trust path;
- can **propose** glossary entries, but only a human binds them.

Every promoted translation is recorded in an append-only, hash-chained
ledger that lives in the repo and travels with git. A stdlib-only external
verifier (`scripts/verify_ledger.py`) re-derives the chain without importing
any koine code.

## What the gate catches

| Situation | State | CI |
|---|---|---|
| Source paragraph edited after translation | `STALE` | exit 1 |
| Doc never translated to a language | `UNTRANSLATED` | exit 1 |
| Translated file edited outside the pipeline | `TAMPERED` | exit 2 |
| Ledger rewritten, reordered, or truncated | broken chain | exit 2 |
| Binding glossary term rendered differently | violation | exit 1 |
| Unreviewed machine translation | `MACHINE_ONLY` | visible, allowed by default |
| Adopted legacy pair, meaning never verified | `LEGACY_UNVERIFIED` | visible, allowed by default |

`MACHINE_ONLY` is koine's honest answer to languages the maintainer cannot
review: the translation ships, labeled as machine-maintained, never
laundered into `CURRENT`. An honest "unverified" beats a confident lie.

## The fleet (Google ADK)

| Agent | May do | May never do |
|---|---|---|
| watcher | read block states, order the work queue | mark anything current |
| translator | translate a frozen template | see or touch protected spans |
| reviewer | REJECT with findings | approve |
| steward | PROPOSE glossary entries | bind them |

Tool contracts are strictly disjoint (`koine/agents/contracts.py`); the only
writers to the ledger are pipeline functions that run mechanical checks first.

## Adopting a repo that already has translations

Nobody starts from zero. `koine adopt` aligns existing translations against
the source by structure, seeds the per-language ledgers, and — crucially —
records every adopted pair as `LEGACY_UNVERIFIED`, never `CURRENT`: alignment
is a hypothesis, and koine refuses to mint trust it cannot back. Orphan
translation blocks (content with no source counterpart, usually sections
deleted from the source but never from the translation) are reported.

```bash
python3 -m koine adopt --source README.md \
  --translation es=README.es.md --translation ru=README.ru.md
python3 -m koine status --source README.md --translation es=README.es.md
```

State lives in `.koine/`: one hash chain per language (so concurrent
per-language PRs never conflict) under a versioned root manifest.

## Quick start

```bash
pip install -e ".[dev]"       # deterministic core: zero dependencies
python3 -m pytest             # tests incl. tamper + determinism + fleet orchestration
python3 -m koine gate --source README.md \
  --translation es=README.es.md --translation ru=README.ru.md
python3 -m koine verify                                # every chain
python3 scripts/verify_ledger.py .koine/ledger.es.jsonl   # external, stdlib-only
python3 scripts/torture.py path/to/any/repo           # freeze/thaw roundtrip

pip install -e ".[agents]"    # the ADK fleet (needs a Gemini credential)
python3 -m koine translate --source README.md --translation es=README.es.md
```

Without ADK or a credential the deterministic pipeline works in full; only
translation *generation* needs a model. The gate never does.

`koine translate --dry-run` needs neither, and is the thing worth running
before you trust a translation pass: it prints the **frozen template** of every
pending block — the exact bytes a model would receive, protected spans already
replaced by `⟦K0⟧`-style placeholders. If a URL or a flag is visible there, the
freeze missed it, and you know before a model ever sees the text.

Code fences and frontmatter are copied into the translated file **verbatim**,
mechanically, with no model involved — a translated README without its code
blocks is not a translated README.

## Glossary

Agents propose; humans bind. The binding is the enforceable act, so the CLI
records who performed it and refuses to do it anonymously:

```bash
python3 -m koine glossary propose --term "ledger" --rendering es=libro --by agent:steward
python3 -m koine glossary bind    --term "ledger" --by anna     # now gate-enforced
python3 -m koine glossary list
```

Use `@same` as a rendering to require a term be kept verbatim in a language.

## GitHub Action

```yaml
- uses: anna/koine/action@v1
  with:
    source: README.md
    translations: "es=README.es.md ru=README.ru.md"
```

The gate fails red the moment any language silently drifts.

## Status

Built during the All Things Agentic Hackathon submission period. Deterministic
core complete and tested. ADK fleet wiring is done end-to-end
(`koine/agents/orchestrator.py`: watcher-selected blocks → translator →
reviewer → mechanical submit, with model calls injectable so the cycle is
tested without any credential), and driveable from the CLI via `koine
translate`; Cloud Run deployment and the GitHub webhook watcher are still in
progress. Pre-existing work: none — see ATTRIBUTIONS.md.

Three defects that produced a **green gate over a broken translation** are
fixed and pinned by regression tests in `tests/test_placement.py`:

| Defect | What it did | Now |
|---|---|---|
| Placement assumed source-index alignment | overwrote an adopted translation's code fence with prose, duplicated the stale paragraph, reported `CURRENT` | placement is read from the ledger; a collision or a missing recorded block is a mechanical rejection |
| `freeze` applied its patterns in sequence | a later pattern swallowed an earlier placeholder (`<a href="https://x">`), so the block could never be thawed and stayed `UNTRANSLATED` forever — **2.1% of 24k real-world markdown blocks** | one non-overlapping left-to-right scan; 0 failures on the same corpus |
| Ledger appends were unlocked | concurrent writers read the same tail and forked the chain | the read-tail-and-write is held under an exclusive lock |

## License

Apache-2.0.
