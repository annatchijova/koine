"""Regressions for the three ways koine could silently lie about a translation.

Each test here corresponds to a defect that produced a *green* gate over a
corrupted or unpromotable translation — the exact failure mode the project
exists to make impossible.
"""
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from koine import state as st
from koine.adopt import adopt
from koine.agents import contracts
from koine.agents.orchestrator import NO_FINDINGS, run_cycle
from koine.blocks import freeze, split_blocks, thaw
from koine.glossary import Glossary
from koine.ledger import Ledger, verify
from koine.store import Store
from koine.__main__ import main as cli


# ---------- freeze must not nest protected spans ----------

def test_html_tag_does_not_swallow_a_url_placeholder():
    """`<a href="https://x">` used to freeze the URL first, then swallow the
    placeholder into the tag span — leaving a template that can never be
    thawed, so the block stayed UNTRANSLATED forever."""
    text = 'See <a href="https://example.org/x">the docs</a> for more.'
    frozen = freeze(text)
    assert thaw(frozen, frozen.template) == text
    assert all("⟦K" not in span for span in frozen.spans)


@pytest.mark.parametrize("text", [
    'Read <a href="https://example.org">docs</a> or run `make test`.',
    '[![badge](https://img.shields.io/x)](https://example.org)',
    'Set GEMINI_API_KEY, pass --verbose, see ./deploy.sh and https://x.io/y.',
    '<img src="./logo.png" alt="https://not-a-link"> after.',
])
def test_freeze_thaw_roundtrip_on_nesting_prone_prose(text):
    frozen = freeze(text)
    assert thaw(frozen, frozen.template) == text


def test_placeholders_are_numbered_in_reading_order():
    """Out-of-order placeholders invite a model to 'fix' the numbering."""
    frozen = freeze("Set GEMINI_API_KEY and read https://example.org/docs.")
    assert frozen.template == "Set ⟦K0⟧ and read ⟦K1⟧."


# ---------- placement must follow the ledger, not the source index ----------

OFFSET_SRC = "Alpha paragraph.\n\n```bash\nmake test\n```\n\nOmega paragraph.\n"
# the translation carries a banner the source has no counterpart for, so its
# block indices are shifted by one relative to the source
OFFSET_TR = ("Nota de traduccion.\n\nParrafo alfa.\n\n"
             "```bash\nmake test\n```\n\nParrafo omega.\n")


def _offset_repo(tmp_path):
    src = tmp_path / "README.md"
    tr = tmp_path / "README.es.md"
    src.write_text(OFFSET_SRC, encoding="utf-8")
    tr.write_text(OFFSET_TR, encoding="utf-8")
    store = Store(tmp_path / ".koine")
    adopt(src, tr, "es", store)
    return src, tr, store.ledger("es")


def test_submit_writes_at_the_adopted_index_not_the_source_index(tmp_path):
    """Writing at the source's own index overwrote the translation's code
    fence with prose, duplicated the stale paragraph, and still reported
    CURRENT. Placement must come from the ledger."""
    src, tr, ledger = _offset_repo(tmp_path)
    src.write_text(OFFSET_SRC.replace("Omega paragraph.", "Omega paragraph, revised."),
                   encoding="utf-8")
    assert [s.state for s in st.derive_states(src, tr, "es", ledger)][2] == st.STALE

    prep = contracts.prepare_block(str(src), 2)
    contracts.submit_candidate(
        source_path=str(src), block_index=2, lang="es",
        translated_template=prep["template"].replace(
            "Omega paragraph, revised.", "Parrafo omega, revisado."),
        ledger=ledger, glossary=Glossary(), reviewed=True,
        translation_file=str(tr))

    out = tr.read_text(encoding="utf-8")
    assert "```bash\nmake test\n```" in out          # the fence survived
    assert "Nota de traduccion." in out              # the banner survived
    assert out.count("Parrafo omega") == 1           # no duplicated paragraph
    assert "Parrafo omega, revisado." in out
    states = st.derive_states(src, tr, "es", ledger)
    assert states[2].state == st.CURRENT
    assert states[0].state == st.LEGACY_UNVERIFIED   # untouched by the write


def test_block_mapping_is_read_from_the_ledger(tmp_path):
    src, tr, ledger = _offset_repo(tmp_path)
    mapping = st.block_mapping(ledger, Path(src).as_posix(), "es")
    assert mapping == {0: 1, 2: 3}   # banner at translation index 0 is an orphan


def test_placement_refuses_to_overwrite_another_blocks_translation(tmp_path):
    src, tr, ledger = _offset_repo(tmp_path)
    raws = [b.raw for b in split_blocks(tr.read_text(encoding="utf-8"))]
    mapping = st.block_mapping(ledger, Path(src).as_posix(), "es")
    mapping[2] = mapping[0]          # forge a collision
    with pytest.raises(contracts.CandidateRejected) as e:
        contracts._place(raws, mapping, 2, "nuevo")
    assert "refusing to overwrite" in str(e.value)


def test_placement_refuses_when_the_recorded_block_is_gone(tmp_path):
    src, tr, ledger = _offset_repo(tmp_path)
    mapping = st.block_mapping(ledger, Path(src).as_posix(), "es")
    with pytest.raises(contracts.CandidateRejected) as e:
        contracts._place(["only one block"], mapping, 2, "nuevo")
    assert "lost content" in str(e.value)


# ---------- never-translated blocks are mirrored, not stubbed ----------

MIRROR_SRC = ("# tool\n\nIntro paragraph.\n\n```bash\ntool --run\n```\n\n"
              "Closing paragraph.\n")


def test_generated_translation_carries_its_code_fences_verbatim(tmp_path):
    src = tmp_path / "README.md"
    tr = tmp_path / "README.es.md"
    src.write_text(MIRROR_SRC, encoding="utf-8")
    run_cycle(source_path=str(src), translation_file=str(tr), lang="es",
              ledger=Ledger(tmp_path / "led.jsonl"), glossary=Glossary(),
              translate_fn=lambda t: t, review_fn=lambda s, c: NO_FINDINGS)
    out = tr.read_text(encoding="utf-8")
    assert "```bash\ntool --run\n```" in out
    assert "koine:untranslated" not in out   # never a stub where code belongs


def test_mirroring_is_idempotent(tmp_path):
    src = tmp_path / "README.md"
    tr = tmp_path / "README.es.md"
    src.write_text(MIRROR_SRC, encoding="utf-8")
    ledger = Ledger(tmp_path / "led.jsonl")
    kw = dict(source_path=str(src), translation_file=str(tr), lang="es",
              ledger=ledger)
    assert contracts.mirror_untranslatable(**kw) == [2]
    assert contracts.mirror_untranslatable(**kw) == []
    assert len(ledger.entries()) == 1


def test_frontmatter_is_never_offered_for_translation(tmp_path):
    src = tmp_path / "README.md"
    src.write_text("---\ntitle: t\n---\n\nBody paragraph.\n", encoding="utf-8")
    with pytest.raises(contracts.CandidateRejected) as e:
        contracts.prepare_block(str(src), 0)
    assert "frontmatter" in str(e.value)


# ---------- one chain, many writers ----------

def _append_batch(args):
    path, worker = args
    ledger = Ledger(path)
    for i in range(20):
        ledger.append(op="translate", doc="README.md", lang="es", block_index=i,
                      source_hash="a" * 64, translation_hash="b" * 64,
                      seq_time="2026-01-01T00:00:00+00:00", meta={"w": worker})


def test_concurrent_writers_cannot_fork_the_chain(tmp_path):
    """Per-language chains exist so concurrent PRs do not conflict, which makes
    concurrent writers to one chain the expected case. Unlocked, they read the
    same tail and emit entries claiming the same prev_hash."""
    path = tmp_path / "chain.jsonl"
    with ProcessPoolExecutor(max_workers=4) as ex:
        list(ex.map(_append_batch, [(str(path), n) for n in range(4)]))
    entries = Ledger(path).entries()
    assert len(entries) == 80
    report = verify(entries)
    assert report["linkage_ok"] and report["integrity_ok"], report["issues"]
    assert [e["seq"] for e in entries] == list(range(80))


# ---------- CLI surface for the fleet and the glossary ----------

def test_cli_translate_dry_run_shows_what_a_model_would_receive(tmp_path, capsys):
    src = tmp_path / "README.md"
    src.write_text(MIRROR_SRC, encoding="utf-8")
    code = cli(["--koine-dir", str(tmp_path / ".koine"), "translate",
                "--source", str(src),
                "--translation", f"es={tmp_path / 'README.es.md'}", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 1                       # pending work is drift, not success
    assert "3 blocks pending" in out
    assert "mirrored 1" in out
    assert "block 2" not in out.split("pending")[1]   # the fence is not offered


def test_cli_glossary_propose_bind_then_gate_enforces(tmp_path, capsys):
    kd = str(tmp_path / ".koine")
    src = tmp_path / "README.md"
    tr = tmp_path / "README.es.md"
    src.write_text("The tool is fast.\n", encoding="utf-8")
    tr.write_text("La herramienta es rapida.\n", encoding="utf-8")

    assert cli(["--koine-dir", kd, "glossary", "propose", "--term", "tool",
                "--rendering", "es=utilidad", "--by", "anna"]) == 0
    cli(["--koine-dir", kd, "glossary", "list"])
    assert "proposed" in capsys.readouterr().out

    # proposed entries are not enforced: only a human binding makes them binding
    store = Store(kd)
    assert Glossary.load(store.glossary_path()).violations(
        src.read_text(), tr.read_text(), "es") == []

    assert cli(["--koine-dir", kd, "glossary", "bind", "--term", "tool",
                "--by", "anna"]) == 0
    violations = Glossary.load(store.glossary_path()).violations(
        src.read_text(), tr.read_text(), "es")
    assert violations and "utilidad" in violations[0]


def test_cli_glossary_bind_requires_a_human_name(tmp_path):
    with pytest.raises(SystemExit):
        cli(["--koine-dir", str(tmp_path / ".koine"), "glossary", "bind",
             "--term", "tool"])


def test_cli_glossary_bind_unknown_term_reports_instead_of_creating(tmp_path, capsys):
    code = cli(["--koine-dir", str(tmp_path / ".koine"), "glossary", "bind",
                "--term", "ghost", "--by", "anna"])
    assert code == 1
    assert "propose it first" in capsys.readouterr().out
