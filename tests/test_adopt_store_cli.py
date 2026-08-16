import json
import subprocess
import sys
from pathlib import Path

from koine.adopt import adopt, align_by_kind
from koine.blocks import freeze, split_blocks, thaw
from koine.ledger import verify
from koine.store import Store, UnsupportedSchema
from koine import state as st
from koine.__main__ import main as cli

FIXTURE = Path(__file__).parent / "fixtures" / "torture.md"

SRC = """# tool

First paragraph explaining the tool.

```bash
tool --run
```

Second paragraph with `code` and details.

Third paragraph, recently added to the source.
"""

# hand-made legacy translation: missing the third paragraph (stale by omission)
TR = """# herramienta

Primer parrafo explicando la herramienta.

```bash
tool --run
```

Segundo parrafo con `code` y detalles.
"""


# ---------- torture roundtrip ----------

def test_torture_fixture_roundtrips_byte_identical():
    text = FIXTURE.read_text(encoding="utf-8")
    blocks = split_blocks(text)
    assert blocks[0].kind == "frontmatter"
    assert any(b.kind == "fence" for b in blocks)
    for b in blocks:
        if b.kind != "text":
            continue
        frozen = freeze(b.raw)
        assert thaw(frozen, frozen.template) == b.raw, f"block {b.index}"


def test_frontmatter_only_at_document_start():
    text = "intro\n\n---\ntitle: not frontmatter\n---\n"
    assert all(b.kind != "frontmatter" for b in split_blocks(text))


def test_torture_script_passes_on_fixture():
    script = Path(__file__).resolve().parents[1] / "scripts" / "torture.py"
    r = subprocess.run([sys.executable, str(script), str(FIXTURE)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ---------- adopt ----------

def _repo(tmp_path):
    (tmp_path / "README.md").write_text(SRC, encoding="utf-8")
    (tmp_path / "README.es.md").write_text(TR, encoding="utf-8")
    return Store(tmp_path / ".koine")


def test_align_by_kind_pairs_common_structure():
    pairs = align_by_kind(["text", "text", "fence", "text", "text"],
                          ["text", "text", "fence", "text"])
    assert (2, 2) in pairs  # fences align
    assert len(pairs) == 4  # last source text block unpaired


def test_adopt_is_honest_about_what_it_knows(tmp_path):
    store = _repo(tmp_path)
    r = adopt(tmp_path / "README.md", tmp_path / "README.es.md", "es", store)
    assert r.adopted == 3           # title + two paragraphs
    assert r.untranslated == 1      # the new third paragraph
    assert r.orphans == []

    states = st.derive_states(tmp_path / "README.md", tmp_path / "README.es.md",
                              "es", store.ledger("es"))
    by_state = {s.state for s in states}
    assert st.LEGACY_UNVERIFIED in by_state     # adopted pairs, never CURRENT
    assert st.CURRENT not in by_state           # adoption cannot mint CURRENT
    assert st.UNTRANSLATED in by_state          # the missing paragraph

    # ledger chain valid and external shape sound
    assert verify(store.ledger("es").entries())["linkage_ok"]


def test_adopted_pair_goes_stale_when_source_changes(tmp_path):
    store = _repo(tmp_path)
    adopt(tmp_path / "README.md", tmp_path / "README.es.md", "es", store)
    src = tmp_path / "README.md"
    src.write_text(SRC.replace("First paragraph", "First (edited) paragraph"),
                   encoding="utf-8")
    states = st.derive_states(src, tmp_path / "README.es.md", "es",
                              store.ledger("es"))
    assert any(s.state == st.STALE for s in states)


# ---------- partitioned store ----------

def test_store_partitions_per_language_with_manifest(tmp_path):
    store = Store(tmp_path / ".koine")
    led_es = store.ledger("es")
    led_ru = store.ledger("ru")
    assert led_es.path.name == "ledger.es.jsonl"
    assert led_ru.path != led_es.path
    manifest = json.loads((tmp_path / ".koine" / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert set(manifest["ledgers"]) == {"es", "ru"}


def test_store_refuses_unknown_schema_instead_of_guessing(tmp_path):
    store = Store(tmp_path / ".koine")
    store.ledger("es")
    m = tmp_path / ".koine" / "manifest.json"
    data = json.loads(m.read_text())
    data["schema_version"] = 99
    m.write_text(json.dumps(data))
    try:
        Store(tmp_path / ".koine").ledger("ru")
        assert False, "should have refused"
    except UnsupportedSchema:
        pass


# ---------- CLI ----------

def test_cli_adopt_status_gate_verify(tmp_path, capsys):
    _repo(tmp_path)
    kd = str(tmp_path / ".koine")
    src = str(tmp_path / "README.md")
    tr = f"es={tmp_path / 'README.es.md'}"

    assert cli(["--koine-dir", kd, "adopt", "--source", src, "--translation", tr]) == 0
    assert cli(["--koine-dir", kd, "status", "--source", src, "--translation", tr]) == 0
    out = capsys.readouterr().out
    assert "LEGACY_UNVERIFIED" in out and "UNTRANSLATED" in out

    # gate: drift present (one untranslated block) → 1, never 0
    assert cli(["--koine-dir", kd, "gate", "--source", src, "--translation", tr]) == 1
    assert cli(["--koine-dir", kd, "verify"]) == 0


def test_gate_never_launders_legacy(tmp_path, capsys):
    _repo(tmp_path)
    kd = str(tmp_path / ".koine")
    src = str(tmp_path / "README.md")
    tr = f"es={tmp_path / 'README.es.md'}"
    cli(["--koine-dir", kd, "adopt", "--source", src, "--translation", tr])
    capsys.readouterr()
    cli(["--koine-dir", kd, "gate", "--source", src, "--translation", tr])
    assert "LEGACY_UNVERIFIED" in capsys.readouterr().out   # visible, siempre
    assert cli(["--koine-dir", kd, "gate", "--source", src, "--translation", tr,
                "--forbid-legacy"]) == 1                     # bloqueable
