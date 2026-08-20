"""GitHub webhook watcher: turns `push` events into a translation work queue.

No model anywhere in this module. Verifying the signature and deriving what
needs translation are both mechanical: signature verification is HMAC-SHA256
over the raw body, and "what needs translation" is exactly `state.py`'s
`derive_states` — the same function the CLI and the gate use, so the
watcher's notion of pending work can never drift from the gate's notion of
current. Per the fleet's contract (see `agents/fleet.py`), the watcher only
orders work; it never marks anything current and it never runs a model
itself. Actually running translations against the queue this emits is a
separate, deployable step (`agents.orchestrator.run_with_adk`) — kept
decoupled so this service needs no model credential to run at all.

Stdlib only, by design: this is the piece meant to run continuously as a
small always-on service (e.g. a Cloud Run job behind the GitHub webhook), so
it should not carry the ADK dependency tree.
"""
from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import state as st
from .ledger import Ledger

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"


class InvalidSignature(Exception):
    pass


def verify_signature(secret: str, payload: bytes, signature_header: str | None) -> None:
    """Raise InvalidSignature unless `signature_header` is a valid HMAC-SHA256
    of `payload` under `secret`, using constant-time comparison. Fails
    closed: a missing secret, a missing header, a malformed header, and a
    wrong signature all raise the same exception — nothing here leaks which
    check failed."""
    if not secret:
        raise InvalidSignature("no webhook secret configured")
    if not signature_header or not signature_header.startswith("sha256="):
        raise InvalidSignature("missing or malformed signature header")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    given = signature_header[len("sha256="):]
    if not hmac.compare_digest(expected, given):
        raise InvalidSignature("signature mismatch")


def extract_changed_paths(push_payload: dict) -> set[str]:
    """Union of added/removed/modified paths across every commit in a GitHub
    push event payload."""
    changed: set[str] = set()
    for commit in push_payload.get("commits", []):
        for key in ("added", "removed", "modified"):
            changed.update(commit.get(key, []))
    return changed


@dataclass
class DocConfig:
    source: str
    translations: dict
    koine_dir: str = ".koine"


@dataclass
class WorkItem:
    doc: str
    lang: str
    block_index: int
    reason: str  # STALE | UNTRANSLATED


def compute_work_queue(doc: DocConfig, repo_root: str | Path = ".") -> list[WorkItem]:
    """Mechanically derive what needs translation for one doc, across every
    configured language."""
    repo_root = Path(repo_root)
    source_path = repo_root / doc.source
    items: list[WorkItem] = []
    for lang, tpath in sorted(doc.translations.items()):
        ledger = Ledger(repo_root / doc.koine_dir / f"ledger.{lang}.jsonl")
        for s in st.derive_states(source_path, repo_root / tpath, lang, ledger):
            if s.state in (st.UNTRANSLATED, st.STALE):
                items.append(WorkItem(doc.source, lang, s.block_index, s.state))
    return items


def docs_affected(docs: list[DocConfig], changed_paths: set) -> list[DocConfig]:
    """Only recompute work for docs whose source or a translation was touched
    by this push."""
    affected = []
    for doc in docs:
        tracked = {doc.source, *doc.translations.values()}
        if tracked & changed_paths:
            affected.append(doc)
    return affected


def handle_push(*, secret: str, signature_header: str | None, body: bytes,
                docs: list, repo_root: str | Path = ".") -> dict:
    """Pure request handler: verify, parse, derive. Raises InvalidSignature
    or json.JSONDecodeError on a bad request; the HTTP adapter maps those to
    status codes without this function knowing anything about HTTP."""
    verify_signature(secret, body, signature_header)
    payload = json.loads(body.decode("utf-8"))
    changed = extract_changed_paths(payload)
    queue: list[WorkItem] = []
    for doc in docs_affected(docs, changed):
        queue.extend(compute_work_queue(doc, repo_root))
    return {
        "changed_paths": sorted(changed),
        "work_queue": [
            {"doc": i.doc, "lang": i.lang, "block_index": i.block_index,
             "reason": i.reason}
            for i in queue
        ],
    }


class WebhookRequestHandler(http.server.BaseHTTPRequestHandler):
    """Thin I/O adapter. All decision logic lives in `handle_push`, which is
    what the tests exercise directly."""
    secret = ""
    docs: list = []
    repo_root = "."

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    def do_GET(self):
        # /livez mirrors /healthz: the Google Front End reserves the exact path
        # /healthz on Cloud Run and never forwards it, so /livez is the health
        # path that works behind GFE. Both answer 200 here.
        if self.path in ("/healthz", "/livez"):
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, {"error": "not found"})
            return
        if self.headers.get(EVENT_HEADER) != "push":
            self._respond(202, {"status": "ignored", "reason": "not a push event"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            result = handle_push(
                secret=self.secret,
                signature_header=self.headers.get(SIGNATURE_HEADER),
                body=body, docs=self.docs, repo_root=self.repo_root,
            )
        except InvalidSignature as e:
            self._respond(401, {"error": str(e)})
            return
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid JSON payload"})
            return
        self._respond(200, result)

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_handler(secret: str, docs: list, repo_root: str | Path):
    class _Handler(WebhookRequestHandler):
        pass
    _Handler.secret = secret
    _Handler.docs = docs
    _Handler.repo_root = str(repo_root)
    return _Handler


def serve(*, host: str = "0.0.0.0", port: int = 8080, secret: str,
         docs: list, repo_root: str | Path = ".") -> None:
    if not secret:
        raise RuntimeError(
            "refusing to start: no webhook secret configured "
            "(set KOINE_WEBHOOK_SECRET)")
    handler_cls = make_handler(secret, docs, repo_root)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    print(f"koine webhook watcher listening on {host}:{port}")
    httpd.serve_forever()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="koine-webhook")
    ap.add_argument("--source", required=True)
    ap.add_argument("--translation", action="append", required=True,
                    help="lang=path, repeatable")
    ap.add_argument("--koine-dir", default=".koine")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    args = ap.parse_args(argv)

    secret = os.environ.get("KOINE_WEBHOOK_SECRET", "")
    translations = dict(t.split("=", 1) for t in args.translation)
    doc = DocConfig(source=args.source, translations=translations,
                    koine_dir=args.koine_dir)
    serve(host=args.host, port=args.port, secret=secret, docs=[doc],
         repo_root=args.repo_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
