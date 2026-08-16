"""CI gate. Exit codes: 0 all current, 1 drift (stale/untranslated),
2 integrity failure (tampered translations or broken ledger chain).

Usage:
    python -m koine.gate --source README.md \
        --translation es=README.es.md --translation ru=README.ru.md \
        --ledger .koine/ledger.jsonl [--allow-machine-only]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import state as st
from .glossary import Glossary
from .ledger import Ledger, verify


def run_gate(source: str, translations: dict, ledger_path: str,
             glossary_path: str | None = None,
             allow_machine_only: bool = True,
             allow_legacy: bool = True) -> int:
    ledger = Ledger(ledger_path)

    chain = verify(ledger.entries())
    if not (chain["linkage_ok"] and chain["integrity_ok"]):
        print("LEDGER BROKEN:")
        for issue in chain["issues"]:
            print(f"  - {issue}")
        return 2

    worst = 0
    glossary = Glossary.load(glossary_path) if glossary_path else Glossary([])
    src_text = Path(source).read_text(encoding="utf-8")

    for lang, path in sorted(translations.items()):
        states = st.derive_states(source, path, lang, ledger)
        for s in states:
            if s.state == st.TAMPERED:
                print(f"[{lang}] block {s.block_index}: TAMPERED — {s.detail}")
                worst = max(worst, 2)
            elif s.state in (st.STALE, st.UNTRANSLATED):
                print(f"[{lang}] block {s.block_index}: {s.state} {s.detail}")
                worst = max(worst, 1)
            elif s.state == st.MACHINE_ONLY and not allow_machine_only:
                print(f"[{lang}] block {s.block_index}: MACHINE_ONLY not allowed here")
                worst = max(worst, 1)
            elif s.state == st.LEGACY_UNVERIFIED:
                # always visible; blocking is opt-in
                print(f"[{lang}] block {s.block_index}: LEGACY_UNVERIFIED — {s.detail}")
                if not allow_legacy:
                    worst = max(worst, 1)

        if Path(path).exists():
            tr_text = Path(path).read_text(encoding="utf-8")
            for v in glossary.violations(src_text, tr_text, lang):
                print(f"[{lang}] glossary: {v}")
                worst = max(worst, 1)

    if worst == 0:
        print("koine gate: all translations current")
    return worst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="koine-gate")
    ap.add_argument("--source", required=True)
    ap.add_argument("--translation", action="append", default=[],
                    help="lang=path, repeatable")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--glossary", default=None)
    ap.add_argument("--forbid-machine-only", action="store_true")
    args = ap.parse_args(argv)

    translations = dict(t.split("=", 1) for t in args.translation)
    return run_gate(args.source, translations, args.ledger, args.glossary,
                    allow_machine_only=not args.forbid_machine_only)


if __name__ == "__main__":
    sys.exit(main())
