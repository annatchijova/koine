"""Read-only web dashboard for koine.

This module renders; it never decides. Every value it shows is produced by
the deterministic engine and sealed before this page is served:
`state.derive_states` derives each block's state from hashes alone, and
`ledger.verify_chain` verifies the per-language hash chain. The dashboard
reads that sealed state and paints it. Swapping the view -- terminal, JSON,
this page -- can never change a verdict, the same property the LLM narrator
layer has. There is no model anywhere in here.

It reads the ledgers directly (`ledger.<lang>.jsonl`, the same files the
webhook watcher reads) and never writes: rendering the dashboard against a
real repo must not mutate its `.koine` store, so it deliberately does not go
through `Store`, whose `ledger()` registers a language in the manifest on
first use.

The gate light mirrors `gate.run_gate` exactly, so the badge on this page and
the exit code of `python -m koine.gate` can never disagree:

    BROKEN  a TAMPERED or UNSOURCED block, or a broken/truncated chain  (exit 2)
    DRIFT   a STALE or UNTRANSLATED block                               (exit 1)
    OK      nothing stale or tampered; tolerated non-current states     (exit 0)
            (MACHINE_ONLY, LEGACY_UNVERIFIED, LEGACY_ORPHAN) are shown,
            never hidden -- an "all current" claim is only made when true.

Stdlib only, by design, like the webhook watcher: this can run continuously
as a small always-on service (e.g. a Cloud Run service beside the webhook)
without carrying the ADK dependency tree.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import state as st
from .ledger import Ledger, verify_chain

# gate severity of each state, mirroring gate.run_gate. 2 fails integrity,
# 1 is drift, 0 passes (tolerated states are 0 but surfaced separately).
GATE_OK = "OK"
GATE_DRIFT = "DRIFT"
GATE_BROKEN = "BROKEN"

_SEVERITY = {
    st.TAMPERED: 2,
    st.UNSOURCED: 2,
    st.STALE: 1,
    st.UNTRANSLATED: 1,
    st.MACHINE_ONLY: 0,
    st.LEGACY_UNVERIFIED: 0,
    st.LEGACY_ORPHAN: 0,
    st.CURRENT: 0,
    st.NOT_TRANSLATABLE: 0,
}

# non-current states the gate tolerates by default: never blocking, always
# shown, so the page cannot imply "all current" when it is not.
_TOLERATED = (st.MACHINE_ONLY, st.LEGACY_UNVERIFIED, st.LEGACY_ORPHAN)


def _gate_level(worst: int) -> str:
    return GATE_BROKEN if worst == 2 else GATE_DRIFT if worst == 1 else GATE_OK


@dataclass
class DashboardConfig:
    """One source document and the translations tracked against it."""
    source: str
    translations: dict  # lang -> path, both relative to repo_root
    koine_dir: str = ".koine"
    title: str = ""


def _lang_snapshot(source_path: Path, translation_path: Path, lang: str,
                   ledger: Ledger) -> dict:
    """Derive everything the UI shows for one (doc, lang), read-only."""
    states = st.derive_states(source_path, translation_path, lang, ledger)

    counts: dict = {}
    tolerated: dict = {}
    blocks: list = []
    queue: list = []
    worst = 0
    for s in states:
        counts[s.state] = counts.get(s.state, 0) + 1
        blocks.append({
            "block_index": s.block_index,
            "side": s.side,
            "state": s.state,
            "detail": s.detail,
        })
        worst = max(worst, _SEVERITY.get(s.state, 0))
        if s.state in (st.STALE, st.UNTRANSLATED):
            queue.append({"block_index": s.block_index, "reason": s.state})
        if s.state in _TOLERATED:
            tolerated[s.state] = tolerated.get(s.state, 0) + 1

    chain = verify_chain(ledger)
    if not chain["ok"]:
        worst = 2  # a broken or truncated chain is an integrity failure

    return {
        "lang": lang,
        "translation_path": translation_path.as_posix(),
        "exists": translation_path.exists(),
        "gate": _gate_level(worst),
        "counts": counts,
        "tolerated": tolerated,
        "blocks": blocks,
        "queue": queue,
        "chain": {
            "ok": chain["ok"],
            "linkage_ok": chain["linkage_ok"],
            "integrity_ok": chain["integrity_ok"],
            "anchored": chain["anchored"],
            "truncated": chain["truncated"],
            "anchor_lag": chain["anchor_lag"],
            "entries": chain["entries"],
            "head": ledger.last_hash(),
            "issues": chain["issues"],
        },
    }, worst


def build_snapshot(configs: list, repo_root: str | Path = ".") -> dict:
    """Pure, JSON-able view of every configured doc/lang. Read-only: opens
    the per-language ledger files by name and never touches the manifest.

    Testable in isolation, exactly as `webhook.handle_push` is -- the HTTP
    handler below only serializes what this returns.
    """
    repo_root = Path(repo_root)
    docs_out: list = []
    overall = 0
    for cfg in configs:
        source_path = repo_root / cfg.source
        langs_out: list = []
        for lang, tpath in sorted(cfg.translations.items()):
            ledger = Ledger(repo_root / cfg.koine_dir / f"ledger.{lang}.jsonl")
            lang_snap, worst = _lang_snapshot(
                source_path, repo_root / tpath, lang, ledger)
            overall = max(overall, worst)
            langs_out.append(lang_snap)
        docs_out.append({
            "source": cfg.source,
            "title": cfg.title or cfg.source,
            "koine_dir": cfg.koine_dir,
            "source_exists": source_path.exists(),
            "langs": langs_out,
        })
    return {
        "note": ("Read-only view. koine derives every state from hashes and "
                 "seals every ledger entry before this page renders; the "
                 "dashboard paints that sealed state and can never alter a "
                 "verdict."),
        "overall_gate": _gate_level(overall),
        "docs": docs_out,
    }


# --------------------------------------------------------------------------
# HTTP adapter. All data assembly lives in build_snapshot, which the tests
# exercise directly; this only routes and serializes.
# --------------------------------------------------------------------------

class DashboardRequestHandler(http.server.BaseHTTPRequestHandler):
    configs: list = []
    repo_root = "."

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._respond(200, PAGE_HTML.encode("utf-8"),
                          "text/html; charset=utf-8")
        elif self.path == "/api/snapshot":
            snap = build_snapshot(self.configs, self.repo_root)
            self._respond(200, json.dumps(snap).encode("utf-8"),
                          "application/json")
        elif self.path == "/healthz":
            self._respond(200, json.dumps({"status": "ok"}).encode("utf-8"),
                          "application/json")
        else:
            self._respond(404, json.dumps({"error": "not found"}).encode("utf-8"),
                          "application/json")

    def _respond(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_handler(configs: list, repo_root: str | Path):
    class _Handler(DashboardRequestHandler):
        pass
    _Handler.configs = configs
    _Handler.repo_root = str(repo_root)
    return _Handler


def serve(*, host: str = "0.0.0.0", port: int = 8081, configs: list,
          repo_root: str | Path = ".") -> None:
    handler_cls = make_handler(configs, repo_root)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    print(f"koine dashboard on http://{host}:{port}  (read-only)")
    httpd.serve_forever()


# --------------------------------------------------------------------------
# Demo data: build a real koine store in a temp dir exhibiting every
# interesting state, so `python -m koine.dashboard --demo` has something to
# paint without touching the user's repo. Uses the real engine end to end --
# nothing here is mocked.
# --------------------------------------------------------------------------

DEMO_SRC = """# Koine Demo

Koine is a deterministic gate that fails red the moment a translation silently drifts from its source.

Every verdict is sealed in a per-language hash chain before this page renders, so the dashboard can only report a result, never invent one.

```bash
koine gate --source README.md --translation es=README.es.md
```

Placement is read from the ledger, never assumed, so a collision is a mechanical rejection rather than a silent overwrite.

This closing paragraph is left untranslated on purpose to demonstrate the UNTRANSLATED state in both languages.
"""


def build_demo(root: str | Path) -> list:
    """Populate *root* with a source, two translations and their ledgers,
    arranged to show CURRENT / MACHINE_ONLY / STALE / UNTRANSLATED /
    TAMPERED / UNSOURCED / NOT_TRANSLATABLE. Returns the dashboard configs.
    """
    from .agents import contracts
    from .blocks import split_blocks
    from .glossary import Glossary

    root = Path(root)
    (root / ".koine").mkdir(parents=True, exist_ok=True)
    src = root / "README.md"
    src.write_text(DEMO_SRC, encoding="utf-8")

    src_blocks = split_blocks(src.read_text(encoding="utf-8"))
    translatable = [b.index for b in src_blocks
                    if b.kind not in st.NEVER_TRANSLATED_KINDS]

    es_tr = root / "README.es.md"
    ru_tr = root / "README.ru.md"
    es_led = Ledger(root / ".koine" / "ledger.es.jsonl")
    ru_led = Ledger(root / ".koine" / "ledger.ru.jsonl")

    def _translate(idx: int, lang: str, tr_path: Path, led: Ledger,
                   reviewed: bool) -> None:
        prep = contracts.prepare_block(str(src), idx)
        contracts.submit_candidate(
            source_path=str(src), block_index=idx, lang=lang,
            translated_template=prep["template"], ledger=led,
            glossary=Glossary(), reviewed=reviewed,
            translation_file=str(tr_path))

    # es: translate the first three translatable blocks; the third is left
    # unreviewed so it surfaces as MACHINE_ONLY.
    es_targets = translatable[:3]
    for i in es_targets:
        _translate(i, "es", es_tr, es_led, reviewed=(i != es_targets[2]))

    # ru: translate everything translatable except one block, which stays
    # UNTRANSLATED (a clean DRIFT column). That skipped block is also the one
    # we will make STALE for es, so mutating the source does not turn a ru
    # translation stale.
    ru_skip = translatable[1]
    for i in translatable:
        if i == ru_skip:
            continue
        _translate(i, "ru", ru_tr, ru_led, reviewed=True)

    # TAMPERED (es): edit a recorded translation block outside the pipeline.
    tamper_anchor = src_blocks[translatable[0]].raw.splitlines()[0]
    es_text = es_tr.read_text(encoding="utf-8")
    es_tr.write_text(
        es_text.replace(tamper_anchor, tamper_anchor + " EDITADO A MANO"),
        encoding="utf-8")

    # UNSOURCED (es): grow the translated file with content no source maps to.
    with es_tr.open("a", encoding="utf-8") as f:
        f.write("\n\nEste parrafo no existe en el documento fuente.\n")

    # STALE (es): change the source block es already translated (and ru
    # skipped) so its recorded source_hash no longer matches.
    stale_anchor = src_blocks[translatable[1]].raw.splitlines()[0]
    src_text = src.read_text(encoding="utf-8")
    src.write_text(
        src_text.replace(stale_anchor, stale_anchor + " (edited upstream)"),
        encoding="utf-8")

    return [DashboardConfig(
        source="README.md",
        translations={"es": "README.es.md", "ru": "README.ru.md"},
        title="README.md (demo)")]


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="koine-dashboard")
    ap.add_argument("--source", help="source document path")
    ap.add_argument("--translation", action="append", default=[],
                    help="lang=path, repeatable")
    ap.add_argument("--koine-dir", default=".koine")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--title", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8081")))
    ap.add_argument("--demo", action="store_true",
                    help="serve a throwaway sample store in a temp dir")
    args = ap.parse_args(argv)

    if args.demo:
        import tempfile
        root = tempfile.mkdtemp(prefix="koine-demo-")
        configs = build_demo(root)
        print(f"koine dashboard: demo store built in {root}")
        serve(host=args.host, port=args.port, configs=configs, repo_root=root)
        return 0

    if not args.source or not args.translation:
        ap.error("--source and at least one --translation are required "
                 "(or use --demo)")
    translations = dict(t.split("=", 1) for t in args.translation)
    cfg = DashboardConfig(source=args.source, translations=translations,
                          koine_dir=args.koine_dir, title=args.title)
    serve(host=args.host, port=args.port, configs=[cfg],
          repo_root=args.repo_root)
    return 0


# --------------------------------------------------------------------------
# The page. Self-contained: inline CSS and JS, no external requests, no build
# step. It fetches /api/snapshot and renders; it holds no logic of its own.
# --------------------------------------------------------------------------

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>koine gate</title>
<style>
  :root{
    --cy:#22d3ee; --in:#6366f1; --deep:#4f46e5;
    --ink:#e7edf7; --md:#9aa8bf; --line:rgba(255,255,255,.14);
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html{scroll-behavior:smooth}
  body{min-height:100vh;font-family:var(--sans);color:var(--ink);line-height:1.5;-webkit-font-smoothing:antialiased;
    background:
      radial-gradient(1100px 640px at 8% -8%, rgba(99,102,241,.30), transparent 60%),
      radial-gradient(900px 560px at 100% 4%, rgba(34,211,238,.16), transparent 58%),
      radial-gradient(760px 520px at 60% 118%, rgba(79,70,229,.18), transparent 60%),
      linear-gradient(160deg,#070a12 0%,#0b1327 55%,#140f34 100%);
    background-attachment:fixed;}
  a{color:var(--cy);text-decoration:none}
  header.top{max-width:1180px;margin:0 auto;padding:24px 26px 6px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
  .brand{display:flex;align-items:center;gap:11px}
  .brand svg{width:30px;height:30px}
  .brand b{font-size:20px;font-weight:800;letter-spacing:.22em;
    background:linear-gradient(135deg,var(--cy),var(--in));-webkit-background-clip:text;background-clip:text;color:transparent}
  .kick{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.2em;color:var(--cy);text-transform:uppercase}
  .sub{color:var(--md);font-size:13px}
  main{max-width:1180px;margin:0 auto;padding:18px 26px 0}
  .note{color:var(--md);font-size:13px;margin:10px 0 18px;max-width:76ch;line-height:1.55}
  .badge{font-family:var(--mono);font-weight:800;font-size:12px;padding:5px 13px;border-radius:999px;letter-spacing:.14em}
  .g-OK{background:rgba(34,197,94,.15);color:#4ade80;border:1px solid rgba(34,197,94,.5)}
  .g-DRIFT{background:rgba(245,158,11,.15);color:#fbbf24;border:1px solid rgba(245,158,11,.5)}
  .g-BROKEN{background:rgba(251,85,112,.15);color:#fb7185;border:1px solid rgba(251,85,112,.55)}
  .doc{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:16px;padding:22px;margin-bottom:22px;
    box-shadow:0 22px 60px rgba(0,0,0,.38)}
  .doc h2{font-size:17px;margin:0 0 4px;font-weight:800;letter-spacing:-.01em}
  .doc .path{color:var(--md);font-family:var(--mono);font-size:12px;margin-bottom:16px}
  .matrix-wrap{overflow-x:auto;margin:8px 0 4px}
  table.matrix{border-collapse:separate;border-spacing:0;font-family:var(--mono);font-size:12px}
  table.matrix th,table.matrix td{border-bottom:1px solid rgba(255,255,255,.07);padding:7px 11px;text-align:center;white-space:nowrap}
  table.matrix thead th{color:var(--md);font-weight:700;font-size:11px;letter-spacing:.08em;text-transform:uppercase}
  table.matrix td.rowhead,table.matrix th.rowhead{color:var(--md);text-align:right;position:sticky;left:0;
    background:rgba(11,19,39,.72);backdrop-filter:blur(4px)}
  .cell{display:inline-block;min-width:56px;padding:3px 8px;border-radius:6px;color:#fff;font-weight:800;
    font-size:11px;letter-spacing:.03em;cursor:default}
  .s-CURRENT{background:#1fa457}.s-MACHINE_ONLY{background:#1594b8}
  .s-LEGACY_UNVERIFIED{background:#57607a}.s-NOT_TRANSLATABLE{background:rgba(255,255,255,.09);color:#9aa8bf}
  .s-STALE{background:#c07d10}.s-UNTRANSLATED{background:#495066}
  .s-TAMPERED{background:#d33b57}.s-UNSOURCED{background:#b1367a}.s-LEGACY_ORPHAN{background:#6d5ef0}
  .cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px;margin-top:18px}
  .card{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.10);border-radius:13px;padding:16px;
    transition:transform .12s ease,border-color .12s ease,background .12s ease}
  .card:hover{transform:translateY(-3px);border-color:rgba(34,211,238,.45);background:rgba(34,211,238,.05)}
  .card h3{margin:0 0 12px;font-size:11px;font-family:var(--mono);color:var(--md);text-transform:uppercase;
    letter-spacing:.16em;display:flex;align-items:center;gap:10px}
  .card h3::after{content:"";flex:1;height:1px;background:linear-gradient(90deg,rgba(34,211,238,.35),transparent)}
  .kv{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:3px 0}
  .kv .k{color:var(--md)} .kv .v{font-family:var(--mono)}
  .pill{font-family:var(--mono);font-size:10.5px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);letter-spacing:.03em}
  .pill.yes{color:#4ade80;border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.08)}
  .pill.no{color:#fb7185;border-color:rgba(251,85,112,.45);background:rgba(251,85,112,.08)}
  .pills{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
  .hash{font-family:var(--mono);font-size:11px;color:var(--md);word-break:break-all}
  .issues{list-style:none;padding:0;margin:8px 0 0}
  .issues li{color:#fbbf24;font-size:12px;font-family:var(--mono);padding:2px 0}
  .pipe{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-family:var(--mono);font-size:12px;margin-bottom:12px}
  .stage{background:rgba(255,255,255,.05);border:1px solid var(--line);border-radius:999px;padding:5px 12px}
  .arrow{color:var(--cy)}
  .queue{list-style:none;padding:0;margin:6px 0 0;font-family:var(--mono);font-size:12px}
  .queue li{padding:2px 0;color:var(--ink)}
  .legend{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 4px}
  .legend .cell{min-width:auto;font-size:11px}
  .ts-list{list-style:none;padding:0;margin:8px 0 0;font-size:12px;font-family:var(--mono)}
  .ts-list li{padding:3px 0}
  footer{padding:36px 26px 54px;text-align:center;font-family:var(--mono);font-size:12px;color:#66748f;letter-spacing:.06em}
  .muted{color:var(--md)}
  :focus-visible{outline:2px solid var(--cy);outline-offset:3px}
  @media (prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto}}
</style>
</head>
<body>
<svg style="position:absolute;width:0;height:0" aria-hidden="true"><defs>
  <linearGradient id="kg" x1="0" y1="0" x2="40" y2="40" gradientUnits="userSpaceOnUse">
    <stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#6366f1"/>
  </linearGradient>
</defs></svg>
<header class="top">
  <div class="brand">
    <svg viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <rect x="9" y="9" width="22" height="22" rx="6" transform="rotate(45 20 20)"
            stroke="url(#kg)" stroke-width="2.5"/>
      <circle cx="20" cy="20" r="4.4" fill="url(#kg)"/>
    </svg>
    <b>KOINE</b>
    <span class="kick">deterministic translation gate</span>
  </div>
  <span id="overall" class="badge">...</span>
  <span class="sub">tamper-evident ledger &middot; LLM out of the decision path</span>
</header>
<main>
  <p class="note" id="note"></p>
  <div class="legend" id="legend"></div>
  <div id="docs"></div>
</main>
<footer>Read-only view. The verdict is sealed before this page renders. <span id="stamp" class="muted"></span></footer>

<script>
const SHORT = {
  CURRENT:"CURRENT", MACHINE_ONLY:"MACHINE", LEGACY_UNVERIFIED:"LEGACY",
  NOT_TRANSLATABLE:"N/T", STALE:"STALE", UNTRANSLATED:"UNTR",
  TAMPERED:"TAMPER", UNSOURCED:"UNSRC", LEGACY_ORPHAN:"ORPHAN"
};
const LEGEND_ORDER = ["CURRENT","MACHINE_ONLY","LEGACY_UNVERIFIED","NOT_TRANSLATABLE",
  "STALE","UNTRANSLATED","TAMPERED","UNSOURCED","LEGACY_ORPHAN"];

function el(tag, cls, html){ const e=document.createElement(tag);
  if(cls) e.className=cls; if(html!=null) e.innerHTML=html; return e; }

function cell(state, detail){
  const c = el("span","cell s-"+state, SHORT[state]||state);
  c.title = state + (detail?(" — "+detail):"");
  return c;
}

function renderLegend(){
  const box = document.getElementById("legend");
  box.innerHTML="";
  for(const s of LEGEND_ORDER){ const c=cell(s,""); c.style.minWidth="auto"; box.appendChild(c); }
}

function chainCard(ch){
  const card = el("div","card");
  card.appendChild(el("h3",null,"Chain of custody"));
  const pills = el("div","pills");
  const p=(label,ok)=>{ const e=el("span","pill "+(ok?"yes":"no"),
     (ok?"✓ ":"✗ ")+label); return e; };
  pills.appendChild(p("linkage", ch.linkage_ok));
  pills.appendChild(p("integrity", ch.integrity_ok));
  pills.appendChild(p("anchored", ch.anchored));
  if(ch.truncated) pills.appendChild(p("truncated", false));
  card.appendChild(pills);
  const kv=(k,v)=>{ const r=el("div","kv"); r.appendChild(el("span","k",k));
    r.appendChild(el("span","v",v)); return r; };
  card.appendChild(kv("entries", ch.entries));
  if(ch.anchor_lag) card.appendChild(kv("anchor lag", ch.anchor_lag));
  const head = el("div","hash", "head "+(ch.head? ch.head.slice(0,16)+"…":"—"));
  card.appendChild(head);
  if(ch.issues && ch.issues.length){
    const ul=el("ul","issues");
    ch.issues.forEach(i=> ul.appendChild(el("li",null, "⚠ "+i)));
    card.appendChild(ul);
  }
  return card;
}

function fleetCard(lang){
  const card = el("div","card");
  card.appendChild(el("h3",null,"Fleet &amp; work queue"));
  const pipe = el("div","pipe");
  ["watcher","translator","reviewer","submit"].forEach((s,i)=>{
    if(i) pipe.appendChild(el("span","arrow","→"));
    pipe.appendChild(el("span","stage",s));
  });
  card.appendChild(pipe);
  const n = lang.queue.length;
  const untr = lang.queue.filter(q=>q.reason==="UNTRANSLATED").length;
  const stale = lang.queue.filter(q=>q.reason==="STALE").length;
  card.appendChild(el("div","kv",
    '<span class="k">pending</span><span class="v">'+n+
    ' ('+untr+' UNTR, '+stale+' STALE)</span>'));
  if(n){
    const ul = el("ul","queue");
    lang.queue.slice(0,8).forEach(q=> ul.appendChild(
      el("li",null,"block "+q.block_index+" · "+q.reason)));
    if(n>8) ul.appendChild(el("li","muted","… and "+(n-8)+" more"));
    card.appendChild(ul);
  } else {
    card.appendChild(el("div","muted","nothing to do — mechanically derived, no model consulted"));
  }
  return card;
}

function translationSideCard(langs){
  const findings=[];
  langs.forEach(l=> l.blocks.filter(b=>b.side==="translation").forEach(b=>
    findings.push({lang:l.lang, ...b})));
  if(!findings.length) return null;
  const card = el("div","card");
  card.appendChild(el("h3",null,"Translation-side findings"));
  const ul = el("ul","ts-list");
  findings.forEach(f=>{
    const li = el("li");
    li.appendChild(cell(f.state, f.detail));
    li.appendChild(document.createTextNode(
      " ["+f.lang+"] translation block "+f.block_index+
      (f.detail?(" — "+f.detail):"")));
    ul.appendChild(li);
  });
  card.appendChild(ul);
  return card;
}

function renderDoc(doc){
  const box = el("div","doc");
  const head = el("div");
  const h2 = el("h2"); h2.textContent = doc.title;
  box.appendChild(h2);
  box.appendChild(el("div","path","source: "+doc.source+" · store: "+doc.koine_dir));

  // matrix: rows = source-side block indices, cols = languages
  const langs = doc.langs;
  const idxSet = new Set();
  const map = {}; // lang -> {idx -> block}
  langs.forEach(l=>{ map[l.lang]={};
    l.blocks.filter(b=>b.side==="source").forEach(b=>{ idxSet.add(b.block_index);
      map[l.lang][b.block_index]=b; }); });
  const rows = [...idxSet].sort((a,b)=>a-b);

  const wrap = el("div","matrix-wrap");
  const table = el("table","matrix");
  const thead = el("thead"); const htr = el("tr");
  htr.appendChild(el("th","rowhead","block"));
  langs.forEach(l=>{
    const th = el("th");
    th.innerHTML = l.lang+' <span class="badge g-'+l.gate+'" style="font-size:10px;padding:2px 6px">'+l.gate+'</span>';
    htr.appendChild(th);
  });
  thead.appendChild(htr); table.appendChild(thead);
  const tbody = el("tbody");
  rows.forEach(idx=>{
    const tr = el("tr");
    tr.appendChild(el("td","rowhead", "#"+idx));
    langs.forEach(l=>{
      const td = el("td");
      const b = map[l.lang][idx];
      if(b) td.appendChild(cell(b.state, b.detail));
      else td.innerHTML='<span class="muted">·</span>';
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody); wrap.appendChild(table); box.appendChild(wrap);

  // per-language cards
  const cols = el("div","cols");
  langs.forEach(l=>{
    const card = el("div","card");
    card.appendChild(el("h3",null, l.lang+" — gate "+l.gate));
    const counts = Object.entries(l.counts).sort();
    counts.forEach(([k,v])=> card.appendChild(el("div","kv",
      '<span class="k">'+(SHORT[k]||k)+'</span><span class="v">'+v+'</span>')));
    const tol = Object.keys(l.tolerated||{});
    if(tol.length){
      card.appendChild(el("div","muted",
        "tolerated (shown, not blocking): "+tol.map(k=>SHORT[k]||k).join(", ")));
    }
    if(!l.exists) card.appendChild(el("div","muted","translation file missing"));
    cols.appendChild(card);
    cols.appendChild(chainCard(l.chain));
    cols.appendChild(fleetCard(l));
  });
  const ts = translationSideCard(langs);
  if(ts) cols.appendChild(ts);
  box.appendChild(cols);
  return box;
}

async function refresh(){
  try{
    const r = await fetch("/api/snapshot", {cache:"no-store"});
    const snap = await r.json();
    const ov = document.getElementById("overall");
    ov.textContent = snap.overall_gate;
    ov.className = "badge g-"+snap.overall_gate;
    document.getElementById("note").textContent = snap.note;
    const docs = document.getElementById("docs");
    docs.innerHTML="";
    snap.docs.forEach(d=> docs.appendChild(renderDoc(d)));
    document.getElementById("stamp").textContent =
      "refreshed "+new Date().toLocaleTimeString();
  }catch(e){
    document.getElementById("overall").textContent="unreachable";
  }
}
renderLegend();
refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
