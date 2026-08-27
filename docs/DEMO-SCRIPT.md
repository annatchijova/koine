# koine — demo video script (~4 minutes)

Target: All Things Agentic Hackathon submission video. The four things the
brief asks the video to prove — problem, value proposition, live application
demo, and proof the backend runs on Google Cloud — are each mapped to a
segment below. Spoken lines are in English (the judging language); stage
directions are notes for the person recording.

Live gate service (show this on screen): https://koine-gate-1028999311218.us-central1.run.app/

---

## [0:00–0:30] The problem

*On screen: an English README beside its translation. Edit one line of the
English; the translation does not change.*

> "Most developers read documentation in a language they didn't grow up in.
> And most translated docs are quietly out of date — which is worse than
> missing, because a stale README lies to you with total confidence. Here's
> the trap: the usual AI answer is a model that both translates *and* decides
> if a translation is current. That model can be fluent, confident, and wrong
> — and leaves nothing you can audit."

## [0:30–1:00] What koine is / value proposition

*On screen: the README title — "Multilingual repos that cannot silently lie."*

> "koine is a deterministic gate over a repo's translations. An agent fleet
> does the translating — but no model ever decides what counts as up to date.
> A translation's state is a pure function of content hashes and a
> tamper-evident ledger. The model proposes; the deterministic core seals.
> Let me show you."

## [1:00–2:45] Live demo — the core

*Large, legible terminal. Run the commands for real; show the exit codes.*

1. "I edit one source paragraph in the README." *(edit the block)*
2. "I run the gate." → `koine gate`
   > "It marks the affected translations **STALE** and fails CI red — exit 1.
   > Nothing shipped a lie." *(show the state and the exit code on screen)*
3. "Now the fleet. This is a real Gemini 3.5 call through Google ADK: a
   translator drafts, an adversarial reviewer that can only *reject* — there
   is no `approve()` — and then a mechanical seal." → `koine translate`
   *(or show the work queue)*
4. "Every protected span — code, URLs, flags — is restored and byte-verified
   before anything is sealed. The model's opinion is never an input to that
   check."
5. "I run verify." → `koine verify`
   > "The ledger is mechanically consistent, hash chain intact." *(show exit 0)*
6. **The clincher:** "The test of the boundary: I swap out the model — or
   remove it entirely — and the sealed verdict, the ledger hash, and the exit
   code don't change. Only *what gets proposed* changes. The LLM is
   structurally out of the decision path."

## [2:45–3:30] Google Cloud proof + architecture

*Switch to a browser with the Cloud Run URL LIVE. The brief asks for this
explicitly — do not skip it.*

> "This runs on Google Cloud. The gate service and dashboard are deployed on
> **Cloud Run** — here's the live URL." *(load the live URL; show the
> read-only dashboard with the state matrix)* "The translation fleet ships as
> a separate Cloud Run Job. Gemini 3.5 via the Google GenAI SDK, orchestrated
> with Google ADK." *(show `docs/architecture.html` for ~2 seconds)*

## [3:30–4:00] Close / honesty

*Back to the terminal or a closing slide.*

> "172 automated tests pass. We even wrote our own red-team document trying to
> break the identity guarantees. And koine is honest about its own limits: for
> languages a maintainer can't review, it ships the translation labeled
> **MACHINE_ONLY** — never laundered into 'current'. An honest 'unverified'
> beats a confident lie. That's the whole product: detect drift, block lies,
> keep the history honest."

---

## Directing notes

- Rehearse step 3 (the real Gemini call): if live latency or the network
  fails on camera, have a pre-recorded backup take — it is the only step with
  an external dependency.
- Show **exit codes on screen** (0/1/2); they are the visual proof the gate is
  mechanical, not cosmetic.
- Step 6 (swapping the model) is the strongest differentiator for
  "Architectural Discipline." Do not compress it.
- Aim to land at 3:45–3:55, not exactly 4:00 — leave some air.
