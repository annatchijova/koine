# koine — a translation gate that cannot silently lie

**Track: Fortified Enterprise Fleet**

## The problem

Multilingual docs rot silently. Someone edits the English README; the Spanish and
Russian copies now say something the source no longer says, and nothing fails —
the CI stays green, the site ships, and readers in other languages are quietly
given stale or wrong information. The usual "AI translation" answer makes this
worse: a model that both translates *and* decides whether a translation is
current can be fluent and confident while being wrong, with no artifact anyone
can audit afterward.

## What koine does

koine is a deterministic gate over a repo's translations, with an agent fleet
that proposes translations but is structurally barred from deciding anything.

For each `(document, language, block)` it derives one of nine states from hashes
alone — `CURRENT`, `STALE`, `TAMPERED`, `UNTRANSLATED`, `MACHINE_ONLY`,
`LEGACY_UNVERIFIED`, `NOT_TRANSLATABLE`, `UNSOURCED`, `LEGACY_ORPHAN` — seals the
result into a per-language SHA-256 hash chain, and fails CI red the moment any
language drifts (exit 0 current / 1 drift / 2 tampered or broken chain). A GitHub
push is turned into a mechanically-derived work queue; an ADK/Gemini fleet drafts
candidates for that queue; every candidate is re-checked mechanically before it is
sealed. A read-only web dashboard renders the sealed state.

## How it works — the decision boundary

The core design rule: the language model is never on the decision path.

- The **deterministic engine** derives each block's state from hashes and seals it
  *before* any model is called. No float touches a sealed value; serialization is
  canonical and versioned; each chain carries an anchor sidecar so a truncated tail
  is detectable, not silently accepted.
- The **fleet** (Google ADK) is four agents with disjoint tools: a *translator*
  (frozen template in, translated template out), an adversarial *reviewer* that can
  only `REJECT` (there is no `approve()`), a *watcher* that orders work, and a
  *steward* that proposes glossary entries. They run on **Gemini 3.5**.
- **Promotion is mechanical.** `submit_candidate` restores and byte-verifies every
  protected span (code, URLs, flags), enforces the binding glossary, reads placement
  from the ledger, and only then seals. The model's opinion is never an input to
  that check.

The test of the boundary: swapping the model backend — or removing it entirely —
changes which translations get *proposed*, never a sealed verdict, a ledger hash,
or the gate's exit code.

## Technologies

- **Gemini 3.5** via the Google GenAI SDK (`google.genai`), reached through Google ADK.
- **Google ADK** (`google-adk`) for the translator / reviewer / watcher / steward fleet.
- **Google Cloud Run** — the gate service (dashboard + GitHub webhook) is deployed as
  a public Cloud Run service; the translation fleet ships as a separate Cloud Run Job.
- **Python standard library only** for the entire decision path and the deployed
  service — no framework in the part that produces the verdict, so the always-on
  service carries neither the ADK dependency tree nor a model credential.

## Data sources

The source and translation Markdown files in a git repository, and koine's own
append-only per-language ledgers (`.koine/ledger.<lang>.jsonl`), which travel in git
and are verifiable with nothing but the standard library. GitHub `push` webhook
payloads drive the work queue. No external dataset; koine reasons over the repo it
guards.

## Fit to the "Fortified Enterprise Fleet" track

- **Registry** — a versioned manifest of per-language ledgers.
- **Runtime** — the ADK orchestrator running the translate → review → seal cycle; in
  CI it closes the loop, retranslating stale blocks and pushing the fix until the gate
  goes green.
- **Memory** — the tamper-evident hash chain, the audited system of record.
- **Security** — HMAC-SHA256 webhook verification (fail-closed); a model that cannot
  promote; content-hash sealing that makes an out-of-band edit show up as `TAMPERED`.
- **Observability** — the dashboard: per-block status matrix, chain-of-custody
  verification, and the live work queue.

## Honest status (what is verified vs. what is built)

- **Verified this build:** 172 automated tests pass; the gate service is deployed to
  Cloud Run and its `/`, `/livez`, and `/api/snapshot` endpoints return 200 over the
  public URL; the dashboard is confirmed read-only by a test that asserts rendering
  never writes to the store; the webhook rejects a bad signature (401) live.
- **Adversarially hardened:** the identity guarantees were red-teamed against
  homoglyph and Unicode-confusable attacks, index-alignment forgery, and ledger
  reordering; the attempts and the closed gaps are written up in `docs/RED-TEAM.md`,
  with a dedicated regression suite (`tests/test_identity_gaps.py`).
- **The autonomous loop, proven on a real pull request:** a PR that edited the source
  drove the full cycle end to end — koine commented on the PR with the block that went
  stale and failed the check red, the ADK/Gemini fleet then retranslated that block
  (a real Gemini call: translator drafts, adversarial reviewer, mechanical seal) and
  pushed the fix, and the check re-ran green. Detect → report → repair → prove, with
  the model proposing and the deterministic core sealing.
- **Not exercised in this build:** the always-on hosted service runs the stdlib gate
  and dashboard only; the fleet runs via `koine translate` (locally and in the
  `koine autofix` CI workflow), not inside the public URL, and is also packaged as a
  Cloud Run Job (`Dockerfile.fleet`).
- **Demo data:** the hosted URL serves a synthetic sample store built through the real
  engine to exhibit every state; it is illustrative, not a production corpus.

## Challenges

Keeping the model out of the decision path while still using it for real work — the
translator's output is treated as an untrusted candidate that must survive mechanical
re-checks, not as an answer. Detecting drift on the *translation* side (content added
to a translated file that no source block maps to) required a second pass the
source-block loop structurally cannot see. Deploying revealed that the Google Front
End reserves the exact path `/healthz` and never forwards it to the container, so the
service now answers liveness on `/livez`.

## What we learned

An audit trail is only worth what its weakest silent-failure mode allows. Most of the
engineering went into the states between PASS and FAIL — surfacing `MACHINE_ONLY`,
`LEGACY_ORPHAN`, and an unanchored chain honestly instead of folding them into a green
check that would have been a confident half-truth.

## Links

- Live gate service: https://koine-gate-1028999311218.us-central1.run.app
- Repository: https://github.com/annatchijova/koine
- Architecture diagram: `docs/architecture.html` in the repo (and a Mermaid version in the README)
- Red-team writeup: `docs/RED-TEAM.md` in the repo
- Demo script: `docs/DEMO-SCRIPT.md` in the repo
