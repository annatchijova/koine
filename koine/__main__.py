"""koine CLI.

    koine adopt     --source README.md --translation es=README.es.md [ru=...]
    koine status    --source README.md --translation es=README.es.md
    koine translate --source README.md --translation es=README.es.md [--dry-run]
    koine gate      --source README.md --translation es=README.es.md [--forbid-machine-only]
    koine notify    --source README.md --translation es=README.es.md   # comment drift on the PR
    koine verify                       # every chain in .koine/
    koine glossary  list | propose | bind

All subcommands take --koine-dir (default .koine). Exit codes follow the
gate convention: 0 clean, 1 drift, 2 integrity failure.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import state as st
from .adopt import adopt
from .gate import run_gate
from .glossary import KEEP_VERBATIM, Glossary
from .ledger import verify_chain
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
        ledger = store.ledger(lang)
        states = st.derive_states(args.source, path, lang, ledger)
        counts: dict = {}
        for s in states:
            counts[s.state] = counts.get(s.state, 0) + 1
        summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
        print(f"[{lang}] {summary}")
        if getattr(args, "explain", False):
            pending = [s for s in states if s.state in (st.STALE, st.UNTRANSLATED)]
            tampered = [s for s in states if s.state in (st.TAMPERED, st.UNSOURCED)]
            tolerated = [s for s in states if s.state in (st.MACHINE_ONLY, st.LEGACY_UNVERIFIED, st.LEGACY_ORPHAN)]
            chain = verify_chain(ledger)
            # BROKEN on any integrity failure: a broken/truncated chain OR a
            # TAMPERED/UNSOURCED block (both are gate exit 2). Without the
            # `tampered` term this printed OK/DRIFT over a tampered translation.
            verdict = "BROKEN" if (not chain["ok"] or tampered) else ("DRIFT" if pending else "OK")
            print(f"    gate: {verdict}")
            print(f"    pending: {len(pending)}; integrity issues: {0 if chain['ok'] else len(chain['issues'])}; tolerated: {len(tolerated)}")
            if pending:
                next_up = ", ".join(
                    f"{s.block_index}:{s.state}" for s in pending[:6]
                )
                print(f"    next: {next_up}" + (" …" if len(pending) > 6 else ""))
            if tampered:
                sample = ", ".join(
                    f"{s.block_index}:{s.state}" for s in tampered[:4]
                )
                print(f"    integrity hits: {sample}" + (" …" if len(tampered) > 4 else ""))
            if tolerated:
                sample = ", ".join(
                    f"{s.block_index}:{s.state}" for s in tolerated[:4]
                )
                print(f"    tolerated: {sample}" + (" …" if len(tolerated) > 4 else ""))
        for s in states:
            if s.state not in (st.CURRENT, st.NOT_TRANSLATABLE):
                where = "translation block" if s.side == "translation" else "block"
                print(f"    {where} {s.block_index}: {s.state}"
                      + (f" — {s.detail}" if s.detail else ""))
    return 0


def cmd_translate(args) -> int:
    """Run the fleet over every pending block, or show what it would be sent.

    --dry-run needs no credential and no ADK: it mirrors the never-translated
    blocks, then prints the frozen template of each pending block — exactly
    the bytes a model would receive, protected spans already replaced. That is
    the thing worth inspecting before trusting a translation run.
    """
    from .agents import contracts
    from .agents.orchestrator import run_with_adk

    store = Store(args.koine_dir)
    glossary = Glossary.load(store.glossary_path())
    worst = 0

    for lang, path in sorted(_parse_translations(args.translation).items()):
        ledger = store.ledger(lang)
        try:
            mirrored = contracts.mirror_untranslatable(
                source_path=args.source, translation_file=path, lang=lang,
                ledger=ledger)
        except contracts.CandidateRejected as e:
            # the translated file no longer matches what the ledger records;
            # writing into it would compound the damage
            print(f"[{lang}] {e}")
            worst = max(worst, 2)
            continue
        if mirrored:
            print(f"[{lang}] mirrored {len(mirrored)} never-translated "
                  f"blocks verbatim (indices {mirrored})")

        if args.dry_run:
            pending = [s for s in st.derive_states(args.source, path, lang, ledger)
                       if s.state in (st.UNTRANSLATED, st.STALE)]
            print(f"[{lang}] {len(pending)} blocks pending")
            for s in pending:
                prep = contracts.prepare_block(args.source, s.block_index)
                print(f"  --- block {s.block_index} ({s.state}) ---")
                print("  " + prep["template"].replace("\n", "\n  "))
            worst = max(worst, 1 if pending else 0)
            continue

        try:
            results = run_with_adk(
                source_path=args.source, translation_file=path, lang=lang,
                source_lang=args.source_lang, ledger=ledger, glossary=glossary,
                model=args.model)
        except RuntimeError as e:  # ADK or credential missing — say so plainly
            print(f"[{lang}] {e}")
            return 1

        for r in results:
            print(f"  block {r.block_index}: {r.outcome}"
                  + (f" — {r.detail}" if r.detail else ""))
        promoted = sum(1 for r in results if r.outcome == "promoted")
        rejected = sum(1 for r in results if r.outcome == "rejected")
        skipped = sum(1 for r in results if r.outcome == "skipped")
        print(f"[{lang}] {promoted}/{len(results)} promoted "
              f"({rejected} rejected, {skipped} skipped)")
        if promoted != len(results):
            worst = max(worst, 1)
    return worst


def cmd_glossary(args) -> int:
    """list / propose / bind. Binding is a human act and is recorded as one."""
    store = Store(args.koine_dir)
    path = store.glossary_path()
    glossary = Glossary.load(path)

    if args.action == "list":
        if not glossary.entries:
            print("glossary empty")
            return 0
        for e in sorted(glossary.entries, key=lambda e: e.term):
            renders = ", ".join(f"{k}={v}" for k, v in sorted(e.renderings.items()))
            print(f"{e.status:8} {e.term!r} -> {renders}"
                  + (f"  [{e.decided_by}]" if e.decided_by else ""))
        return 0

    if args.action == "propose":
        renderings = _parse_translations(args.rendering)
        glossary.propose(args.term, renderings, by=args.by)
        glossary.save(path)
        print(f"proposed {args.term!r}; only a human binding makes it enforced "
              f"— run: koine glossary bind --term {args.term!r} --by <name>")
        return 0

    # bind
    try:
        glossary.bind(args.term, by=args.by)
    except KeyError:
        print(f"no glossary entry for {args.term!r}; propose it first")
        return 1
    glossary.save(path)
    print(f"bound {args.term!r} by {args.by}; the gate now enforces it "
          f"({KEEP_VERBATIM} means keep the source term verbatim)")
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
        report = verify_chain(store.ledger(lang))
        if report["ok"]:
            # never a bare VERIFIED while something is off: an unanchored chain
            # is intact but its tail could have been truncated without leaving a
            # trace, and a lagging anchor means a write did not finish
            if not report["anchored"]:
                status = "VERIFIED (unanchored: truncation undetectable)"
            elif report["anchor_lag"]:
                status = (f"VERIFIED (anchor lags by {report['anchor_lag']}; "
                          f"the next append re-anchors)")
            else:
                status = "VERIFIED"
        else:
            status = "TRUNCATED" if report["truncated"] else "BROKEN"
        print(f"[{lang}] {status} — {report['entries']} entries")
        for issue in report["issues"]:
            print(f"    - {issue}")
        if not report["ok"]:
            worst = 2
    return worst


def cmd_notify(args) -> int:
    """Report drift to a human: a comment on the PR (or the pushed commit) and,
    if configured, Slack and email. Built for CI -- it reads the PR context from
    the GitHub Actions environment and the channels from NotifyConfig.from_env.
    It only reports the mechanically-derived queue; it decides nothing, and it
    no-ops cleanly when there is no drift or no channel is configured."""
    import json as _json

    from . import notify
    from .webhook import DocConfig, compute_work_queue

    doc = DocConfig(source=args.source,
                    translations=_parse_translations(args.translation),
                    koine_dir=args.koine_dir)
    queue = compute_work_queue(doc, repo_root=".")
    if not queue:
        print("koine notify: no drift, nothing to report")
        return 0

    cfg = notify.NotifyConfig.from_env()
    if not cfg.any():
        print("koine notify: drift found but no channel configured "
              "(set KOINE_GITHUB_TOKEN / KOINE_SLACK_WEBHOOK_URL / SMTP vars)")
        return 1 if args.fail_on_drift else 0

    repo = os.environ.get("GITHUB_REPOSITORY")
    sha = os.environ.get("GITHUB_SHA")
    branch = os.environ.get("GITHUB_HEAD_REF") or None
    pr_number = None
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and Path(event_path).exists():
        try:
            data = _json.loads(Path(event_path).read_text(encoding="utf-8"))
            pr_number = (data.get("pull_request") or {}).get("number")
        except (ValueError, OSError):
            pr_number = None

    queue_dicts = [{"doc": i.doc, "lang": i.lang, "block_index": i.block_index,
                    "reason": i.reason} for i in queue]
    report = notify.format_report([], queue_dicts, repo=repo, branch=branch)
    statuses = notify.dispatch(report, cfg, context={
        "repo": repo, "branch": branch, "sha": sha, "pr_number": pr_number})
    for ch, status in sorted(statuses.items()):
        print(f"  {ch}: {status}")
    return 1 if args.fail_on_drift else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="koine")
    ap.add_argument("--koine-dir", default=".koine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name in ("adopt", "status", "translate", "gate", "notify", "verify"):
        p = sub.add_parser(name)
        if name != "verify":
            p.add_argument("--source", required=True)
            p.add_argument("--translation", action="append", required=True,
                           help="lang=path, repeatable")
        if name == "gate":
            p.add_argument("--forbid-machine-only", action="store_true")
            p.add_argument("--forbid-legacy", action="store_true")
        if name == "notify":
            p.add_argument("--fail-on-drift", action="store_true",
                           help="exit 1 when drift was reported (for a blocking check)")
        if name == "translate":
            p.add_argument("--source-lang", default="en")
            p.add_argument("--model", default="gemini-3.5-flash")
            p.add_argument("--dry-run", action="store_true",
                           help="show the frozen templates instead of calling a model")
        if name == "status":
            p.add_argument("--explain", action="store_true",
                           help="print a short operational summary before the block list")

    g = sub.add_parser("glossary")
    g.add_argument("action", choices=["list", "propose", "bind"])
    g.add_argument("--term")
    g.add_argument("--rendering", action="append", default=[],
                   help=f"lang=rendering, repeatable ({KEEP_VERBATIM} = keep verbatim)")
    g.add_argument("--by", default="", help="who is proposing or binding")

    args = ap.parse_args(argv)
    if args.cmd == "glossary":
        if args.action in ("propose", "bind") and not args.term:
            ap.error(f"glossary {args.action} needs --term")
        if args.action == "bind" and not args.by:
            ap.error("glossary bind needs --by: binding is a human decision "
                     "and the glossary records who made it")

    return {"adopt": cmd_adopt, "status": cmd_status, "translate": cmd_translate,
            "gate": cmd_gate, "notify": cmd_notify, "verify": cmd_verify,
            "glossary": cmd_glossary}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
