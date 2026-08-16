"""koine CLI.

    koine adopt  --source README.md --translation es=README.es.md [ru=...]
    koine status --source README.md --translation es=README.es.md
    koine gate   --source README.md --translation es=README.es.md [--forbid-machine-only]
    koine verify                       # every chain in .koine/

All subcommands take --koine-dir (default .koine). Exit codes follow the
gate convention: 0 clean, 1 drift, 2 integrity failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import state as st
from .adopt import adopt
from .gate import run_gate
from .glossary import Glossary
from .ledger import verify
from .store import Store


def _parse_translations(items: list[str]) -> dict:
    return dict(t.split("=", 1) for t in items)


def cmd_adopt(args) -> int:
    store = Store(args.koine_dir)
    worst = 0
    for lang, path in sorted(_parse_translations(args.translation).items()):
        if not Path(path).exists():
            print(f"[{lang}] {path}: file not found, skipped")
            worst = max(worst, 1)
            continue
        r = adopt(args.source, path, lang, store)
        print(f"[{lang}] adopted {r.adopted} blocks as LEGACY_UNVERIFIED, "
              f"{r.untranslated} untranslated", end="")
        if r.orphans:
            print(f", {len(r.orphans)} orphan translation blocks "
                  f"(indices {r.orphans}) — content with no source counterpart")
        else:
            print()
    return worst


def cmd_status(args) -> int:
    store = Store(args.koine_dir)
    for lang, path in sorted(_parse_translations(args.translation).items()):
        states = st.derive_states(args.source, path, lang, store.ledger(lang))
        counts: dict = {}
        for s in states:
            counts[s.state] = counts.get(s.state, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        print(f"[{lang}] {summary}")
        for s in states:
            if s.state not in (st.CURRENT, st.NOT_TRANSLATABLE):
                print(f"    block {s.block_index}: {s.state}"
                      + (f" — {s.detail}" if s.detail else ""))
    return 0


def cmd_gate(args) -> int:
    store = Store(args.koine_dir)
    worst = 0
    glossary_path = store.glossary_path()
    for lang, path in sorted(_parse_translations(args.translation).items()):
        code = run_gate(
            args.source, {lang: path}, str(store.ledger(lang).path),
            str(glossary_path) if glossary_path.exists() else None,
            allow_machine_only=not args.forbid_machine_only,
            allow_legacy=not args.forbid_legacy,
        )
        worst = max(worst, code)
    return worst


def cmd_verify(args) -> int:
    store = Store(args.koine_dir)
    langs = store.languages()
    if not langs:
        print("no ledgers registered")
        return 0
    worst = 0
    for lang in langs:
        report = verify(store.ledger(lang).entries())
        ok = report["linkage_ok"] and report["integrity_ok"]
        print(f"[{lang}] {'VERIFIED' if ok else 'BROKEN'}")
        for issue in report["issues"]:
            print(f"    - {issue}")
        if not ok:
            worst = 2
    return worst


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="koine")
    ap.add_argument("--koine-dir", default=".koine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, needs_docs in (("adopt", True), ("status", True),
                             ("gate", True), ("verify", False)):
        p = sub.add_parser(name)
        if needs_docs:
            p.add_argument("--source", required=True)
            p.add_argument("--translation", action="append", required=True,
                           help="lang=path, repeatable")
        if name == "gate":
            p.add_argument("--forbid-machine-only", action="store_true")
            p.add_argument("--forbid-legacy", action="store_true")

    args = ap.parse_args(argv)
    return {"adopt": cmd_adopt, "status": cmd_status,
            "gate": cmd_gate, "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
