from koine.agents.orchestrator import NO_FINDINGS, run_cycle
from koine.glossary import Glossary
from koine.ledger import Ledger
from koine import state as st

SRC = """# annaconda

Run the tests with `PYTHONPATH=$(pwd) python3 -m pytest` before deploying.

```bash
gcloud run deploy vigia-live --region us-central1
```

Set GEMINI_API_KEY and use the --verbose flag. Docs at https://example.org/x.
"""


def _setup_repo(tmp_path):
    src = tmp_path / "README.md"
    src.write_text(SRC, encoding="utf-8")
    return src, tmp_path / "README.es.md", Ledger(tmp_path / "ledger.jsonl")


def _identity_translate(template: str) -> str:
    return template  # placeholders untouched, prose "translated" to itself


def test_clean_run_promotes_every_non_fence_block(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    results = run_cycle(
        source_path=str(src), translation_file=str(tr), lang="es",
        ledger=led, glossary=Glossary(),
        translate_fn=_identity_translate, review_fn=lambda s, c: NO_FINDINGS,
    )
    outcomes = {r.block_index: r.outcome for r in results}
    assert outcomes[0] == "promoted"
    assert outcomes[1] == "promoted"
    assert 2 not in outcomes  # the fenced code block is never eligible, not even attempted
    assert outcomes[3] == "promoted"

    states = st.derive_states(src, tr, "es", led)
    assert states[0].state == st.CURRENT
    assert states[1].state == st.CURRENT
    assert states[2].state == st.NOT_TRANSLATABLE


def test_reviewer_rejection_blocks_promotion_and_touches_no_ledger_entry(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    results = run_cycle(
        source_path=str(src), translation_file=str(tr), lang="es",
        ledger=led, glossary=Glossary(),
        translate_fn=_identity_translate,
        review_fn=lambda s, c: "REJECT: negation dropped",
    )
    assert results and all(r.outcome == "rejected" for r in results)
    assert list(led.entries()) == []

    states = st.derive_states(src, tr, "es", led)
    assert states[0].state == st.UNTRANSLATED
    assert states[1].state == st.UNTRANSLATED


def test_dropped_placeholder_is_caught_mechanically_even_if_reviewer_approves(tmp_path):
    src, tr, led = _setup_repo(tmp_path)

    def drop_first_placeholder(template: str) -> str:
        return template.replace("⟦K0⟧", "", 1)

    results = run_cycle(
        source_path=str(src), translation_file=str(tr), lang="es",
        ledger=led, glossary=Glossary(),
        translate_fn=drop_first_placeholder, review_fn=lambda s, c: NO_FINDINGS,
    )
    block1 = next(r for r in results if r.block_index == 1)
    assert block1.outcome == "rejected"
    assert "protected spans" in block1.detail or "spans missing" in block1.detail


def test_second_pass_only_touches_blocks_still_pending(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    first = run_cycle(
        source_path=str(src), translation_file=str(tr), lang="es",
        ledger=led, glossary=Glossary(),
        translate_fn=_identity_translate, review_fn=lambda s, c: NO_FINDINGS,
    )
    assert {r.block_index for r in first if r.outcome == "promoted"} == {0, 1, 3}

    second = run_cycle(
        source_path=str(src), translation_file=str(tr), lang="es",
        ledger=led, glossary=Glossary(),
        translate_fn=_identity_translate, review_fn=lambda s, c: NO_FINDINGS,
    )
    assert second == []  # everything is CURRENT or NOT_TRANSLATABLE; nothing pending
