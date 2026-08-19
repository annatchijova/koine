"""Randomised document mutation: does the pipeline stay honest and converge?

Hand-written cases test the failures you thought of. This one edits a document
the way people actually do — insert, delete, edit, move — and after every edit
asserts the four properties that together mean "koine did its job":

  1. nothing is left pending (every block CURRENT or NOT_TRANSLATABLE),
  2. the gate agrees and exits 0,
  3. the translated file really says what the source says, block for block,
  4. a second cycle changes nothing (no re-translation, no drift).

It is what caught the pipeline having no way to remove a translation whose
source section was deleted, and what showed that a content-matching scheme
tried in its place broke convergence on 95 of 120 seeds.
"""
import io
import contextlib
import random

from koine import state as st
from koine.agents.orchestrator import NO_FINDINGS, run_cycle
from koine.blocks import split_blocks
from koine.gate import run_gate
from koine.glossary import Glossary
from koine.store import Store

WORDS = ["alfa", "beta", "gamma", "delta", "epsilon", "zeta"]


def _make_block(rng, n):
    r = rng.random()
    if r < 0.15:
        return f"```bash\ncmd-{n} --flag\n```"
    if r < 0.30:
        return f"## Seccion {n} con `code-{n}` y https://ex.org/{n}"
    return f"Parrafo {n} sobre {rng.choice(WORDS)} con `flag-{n}`."


def _doc(blocks):
    return "\n\n".join(blocks) + "\n"


def _mutate(rng, blocks, nxt):
    op = rng.choice(["insert", "delete", "edit", "move", "insert", "edit"])
    if op == "insert" or not blocks:
        blocks.insert(rng.randrange(len(blocks) + 1), _make_block(rng, next(nxt)))
    elif op == "delete" and len(blocks) > 1:
        blocks.pop(rng.randrange(len(blocks)))
    elif op == "edit":
        i = rng.randrange(len(blocks))
        if not blocks[i].startswith("```"):
            blocks[i] += f" Editado-{next(nxt)}."
    elif op == "move" and len(blocks) > 2:
        blocks.insert(rng.randrange(len(blocks)), blocks.pop(rng.randrange(len(blocks))))


def _cycle(src, tr, ledger, calls):
    def translate(template):
        calls.append(template)
        return "[es] " + template

    run_cycle(source_path=str(src), translation_file=str(tr), lang="es",
              ledger=ledger, glossary=Glossary(),
              translate_fn=translate, review_fn=lambda s, c: NO_FINDINGS)


def _check(src, tr, ledger, seed, step):
    pending = [(s.block_index, s.side, s.state)
               for s in st.derive_states(src, tr, "es", ledger)
               if s.state not in (st.CURRENT, st.NOT_TRANSLATABLE)]
    assert not pending, f"seed {seed} step {step}: not converged -> {pending}"

    with contextlib.redirect_stdout(io.StringIO()):
        code = run_gate(str(src), {"es": str(tr)}, str(ledger.path))
    assert code == 0, f"seed {seed} step {step}: gate exit {code}"

    sb = split_blocks(src.read_text(encoding="utf-8"))
    tb = split_blocks(tr.read_text(encoding="utf-8"))
    mapping = st.block_mapping(ledger, src.as_posix(), "es")
    slots = []
    for b in sb:
        j = mapping.get(b.index)
        assert j is not None, f"seed {seed} step {step}: block {b.index} unmapped"
        want = b.raw if b.kind != "text" else "[es] " + b.raw
        assert tb[j].raw == want, (
            f"seed {seed} step {step}: block {b.index} -> translation block {j}\n"
            f"  want {want!r}\n  got  {tb[j].raw!r}")
        slots.append(j)
    assert len(set(slots)) == len(slots), (
        f"seed {seed} step {step}: two source blocks share a translation slot")


def test_pipeline_converges_under_random_document_edits(tmp_path):
    for seed in range(20):
        rng = random.Random(seed)
        nxt = iter(range(10_000))
        room = tmp_path / f"seed{seed}"
        room.mkdir()
        src, tr = room / "R.md", room / "R.es.md"
        ledger = Store(room / ".koine").ledger("es")

        blocks = [_make_block(rng, next(nxt)) for _ in range(rng.randint(3, 8))]
        src.write_text(_doc(blocks), encoding="utf-8")

        for step in range(6):
            _cycle(src, tr, ledger, [])
            _check(src, tr, ledger, seed, step)

            second: list = []
            _cycle(src, tr, ledger, second)
            assert not second, (
                f"seed {seed} step {step}: not idempotent, re-translated {second}")

            _mutate(rng, blocks, nxt)
            src.write_text(_doc(blocks), encoding="utf-8")
