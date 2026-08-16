import json
import subprocess
import sys
from pathlib import Path

import pytest

from koine.blocks import freeze, split_blocks, thaw, verify_protected_spans
from koine.canonical import FloatInSealedPayload, seal, seal_text
from koine.glossary import Glossary
from koine.ledger import GENESIS, Ledger, verify
from koine import state as st
from koine.agents.contracts import CandidateRejected, submit_candidate, prepare_block
from koine.gate import run_gate

SRC = """# annaconda

Run the tests with `PYTHONPATH=$(pwd) python3 -m pytest` before deploying.

```bash
gcloud run deploy vigia-live --region us-central1
```

Set GEMINI_API_KEY and use the --verbose flag. Docs at https://example.org/x.
"""


# ---------- canonical ----------

def test_seal_is_stable_and_typed():
    assert seal({"a": 1}) == seal({"a": 1})
    assert seal({"a": 1}) != seal({"a": "1"})
    assert seal({"a": True}) != seal({"a": 1})
    assert seal({"a": 1, "b": 2}) == seal({"b": 2, "a": 1})


def test_float_is_rejected():
    with pytest.raises(FloatInSealedPayload):
        seal({"score": 0.5})


# ---------- blocks ----------

def test_fences_are_single_untranslated_blocks():
    blocks = split_blocks(SRC)
    fences = [b for b in blocks if b.kind == "fence"]
    assert len(fences) == 1
    assert "gcloud run deploy" in fences[0].raw


def test_freeze_protects_code_env_flags_urls():
    text = "Run `pytest -q` with GEMINI_API_KEY set, pass --verbose, see https://example.org/x."
    frozen = freeze(text)
    for span in ("`pytest -q`", "GEMINI_API_KEY", "--verbose", "https://example.org/x"):
        assert span in frozen.spans
        assert span not in frozen.template


def test_thaw_roundtrip_and_mechanical_rejection():
    text = "Use `pytest -q` and the --verbose flag."
    frozen = freeze(text)
    assert thaw(frozen, frozen.template) == text
    with pytest.raises(ValueError):
        thaw(frozen, frozen.template.replace("\u27e6K0\u27e7", ""))  # dropped placeholder


def test_verify_protected_spans_catches_broken_command():
    src = "Run `PYTHONPATH=$(pwd) python3 -m pytest` now."
    bad = "Ejecutá `PYTHONPATH=$(pwd) python -m pytest` ahora."  # python3 → python
    assert verify_protected_spans(src, bad)


# ---------- glossary ----------

def test_glossary_binding_enforced_proposed_ignored(tmp_path):
    g = Glossary()
    g.propose("Indicator of Intent", {"es": "Indicador de Intención"}, by="steward")
    assert g.violations("An Indicator of Intent.", "Un indicador cualquiera.", "es") == []
    g.bind("Indicator of Intent", by="anna")
    assert g.violations("An Indicator of Intent.", "Un indicador cualquiera.", "es")
    assert g.violations("An Indicator of Intent.", "Un Indicador de Intención.", "es") == []
    p = tmp_path / "g.json"
    g.save(p)
    assert Glossary.load(p).entries[0].status == "binding"


# ---------- ledger ----------

def _append(ledger, i, sh="s" * 64, th="t" * 64):
    return ledger.append(op="translate", doc="README.md", lang="es", block_index=i,
                         source_hash=sh, translation_hash=th, seq_time=f"2026-08-14T00:00:0{i}+00:00")


def test_chain_appends_verifies_and_catches_tampering(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    for i in range(3):
        _append(led, i)
    assert verify(led.entries()) == {"linkage_ok": True, "integrity_ok": True, "issues": []}

    # tamper: edit a payload field in place
    lines = led.path.read_text().splitlines()
    e = json.loads(lines[1]); e["payload"]["lang"] = "ru"
    lines[1] = json.dumps(e, sort_keys=True)
    led.path.write_text("\n".join(lines) + "\n")
    report = verify(led.entries())
    assert not report["integrity_ok"]

    # deletion breaks linkage
    led.path.write_text(lines[0] + "\n" + lines[2] + "\n")
    report = verify(led.entries())
    assert not report["linkage_ok"]


def test_genesis_is_unique(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    e0 = _append(led, 0)
    assert e0["prev_hash"] == GENESIS
    e1 = _append(led, 1)
    assert e1["prev_hash"] == e0["audit_hash"]


def test_external_verifier_agrees(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(3):
        _append(led, i)
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_ledger.py"
    r = subprocess.run([sys.executable, str(script), str(led.path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "VERIFIED" in r.stdout


# ---------- pipeline + state + gate ----------

def _setup_repo(tmp_path):
    src = tmp_path / "README.md"
    src.write_text(SRC, encoding="utf-8")
    return src, tmp_path / "README.es.md", Ledger(tmp_path / "ledger.jsonl")


def test_candidate_with_broken_span_is_rejected_mechanically(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    prep = prepare_block(str(src), 1)
    with pytest.raises(CandidateRejected):
        submit_candidate(source_path=str(src), block_index=1, lang="es",
                         translated_template=prep["template"].replace("\u27e6K0\u27e7", "`pytest`"),
                         ledger=led, glossary=Glossary(), reviewed=True,
                         translation_file=str(tr))


def test_full_flow_current_then_stale_then_gate(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    # translate block 1 (the paragraph) keeping placeholders intact
    prep = prepare_block(str(src), 1)
    translated = prep["template"].replace("Run the tests with", "Corré los tests con").replace("before deploying", "antes de desplegar")
    submit_candidate(source_path=str(src), block_index=1, lang="es",
                     translated_template=translated, ledger=led,
                     glossary=Glossary(), reviewed=True, translation_file=str(tr))

    states = st.derive_states(src, tr, "es", led)
    assert states[1].state == st.CURRENT
    assert states[0].state == st.UNTRANSLATED

    # gate: drift present (other blocks untranslated) → exit 1
    assert run_gate(str(src), {"es": str(tr)}, str(led.path)) == 1

    # source paragraph changes → STALE, mechanically
    src.write_text(SRC.replace("before deploying", "before shipping"), encoding="utf-8")
    states = st.derive_states(src, tr, "es", led)
    assert states[1].state == st.STALE

    # editing the translation outside the pipeline → TAMPERED → exit 2
    src.write_text(SRC, encoding="utf-8")
    tr.write_text(tr.read_text(encoding="utf-8").replace("Corré", "Corre"), encoding="utf-8")
    states = st.derive_states(src, tr, "es", led)
    assert states[1].state == st.TAMPERED
    assert run_gate(str(src), {"es": str(tr)}, str(led.path)) == 2


def test_machine_only_is_visible_not_hidden(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    prep = prepare_block(str(src), 1)
    submit_candidate(source_path=str(src), block_index=1, lang="es",
                     translated_template=prep["template"], ledger=led,
                     glossary=Glossary(), reviewed=False, translation_file=str(tr))
    states = st.derive_states(src, tr, "es", led)
    assert states[1].state == st.MACHINE_ONLY


def test_fence_is_not_translatable_not_untranslated_and_never_blocks_the_gate(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    # translate every non-fence block, leave the fence alone
    for i in (0, 1, 3):
        prep = prepare_block(str(src), i)
        submit_candidate(source_path=str(src), block_index=i, lang="es",
                         translated_template=prep["template"], ledger=led,
                         glossary=Glossary(), reviewed=True, translation_file=str(tr))

    states = st.derive_states(src, tr, "es", led)
    fence_state = next(s for s in states if s.block_index == 2)
    assert fence_state.state == st.NOT_TRANSLATABLE
    assert fence_state.state not in (st.UNTRANSLATED, st.STALE)

    # nothing pending anymore: the gate must report clean, not "1 drift"
    assert run_gate(str(src), {"es": str(tr)}, str(led.path)) == 0
