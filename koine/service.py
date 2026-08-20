"""Single Cloud Run entrypoint: one HTTP server, one port, serving both the
read-only dashboard and the GitHub webhook watcher.

Cloud Run hands a container exactly one port ($PORT). By default koine runs
the dashboard (a read surface) and the webhook watcher (a write-trigger) as
two separate servers, because they are two different trust roles. A single
Cloud Run service needs one listener, so this module composes their two pure
cores behind one router -- without merging the roles:

  GET  /             -> dashboard page          (dashboard.PAGE_HTML)
  GET  /api/snapshot -> sealed-state JSON        (dashboard.build_snapshot)
  GET  /healthz      -> liveness
  POST /webhook      -> push event -> work queue (webhook.handle_push)

Still stdlib-only, and still no model: the always-on deployed service carries
neither the ADK dependency tree nor a Gemini credential. Running the actual
translation fleet (agents.orchestrator.run_with_adk) is a separate credentialed
step, deployed as its own Cloud Run Job, kept decoupled exactly as the webhook
module documents. The dashboard needs no secret and serves whether or not a
webhook secret is configured; POST /webhook fails closed without one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import dashboard as dash
from . import webhook as wh


def _webhook_docs_from(configs: list) -> list:
    """Derive the webhook's DocConfig list from the dashboard configs, so the
    two surfaces are guaranteed to describe the same documents."""
    return [wh.DocConfig(source=c.source, translations=dict(c.translations),
                         koine_dir=c.koine_dir) for c in configs]


class ServiceRequestHandler(dash.DashboardRequestHandler):
    """Extends the dashboard handler (GET routes) with the webhook POST route.
    All decision logic still lives in the two pure cores; this only routes."""
    secret = ""
    webhook_docs: list = []

    def do_POST(self):
        if self.path != "/webhook":
            self._respond(404, json.dumps({"error": "not found"}).encode("utf-8"),
                          "application/json")
            return
        if self.headers.get(wh.EVENT_HEADER) != "push":
            self._respond(202, json.dumps(
                {"status": "ignored", "reason": "not a push event"}).encode("utf-8"),
                "application/json")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            result = wh.handle_push(
                secret=self.secret,
                signature_header=self.headers.get(wh.SIGNATURE_HEADER),
                body=body, docs=self.webhook_docs, repo_root=self.repo_root,
            )
        except wh.InvalidSignature as e:
            self._respond(401, json.dumps({"error": str(e)}).encode("utf-8"),
                          "application/json")
            return
        except json.JSONDecodeError:
            self._respond(400, json.dumps({"error": "invalid JSON payload"}).encode("utf-8"),
                          "application/json")
            return
        self._respond(200, json.dumps(result).encode("utf-8"), "application/json")


def make_handler(configs: list, repo_root: str | Path, secret: str):
    class _Handler(ServiceRequestHandler):
        pass
    _Handler.configs = configs
    _Handler.repo_root = str(repo_root)
    _Handler.secret = secret
    _Handler.webhook_docs = _webhook_docs_from(configs)
    return _Handler


def serve(*, host: str = "0.0.0.0", port: int = 8080, configs: list,
          repo_root: str | Path = ".", secret: str = "") -> None:
    import http.server
    handler_cls = make_handler(configs, repo_root, secret)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    surface = "dashboard + webhook" if secret else "dashboard (webhook fails closed: no secret)"
    print(f"koine service on http://{host}:{port}  [{surface}]")
    httpd.serve_forever()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="koine-service")
    ap.add_argument("--source", help="source document path")
    ap.add_argument("--translation", action="append", default=[],
                    help="lang=path, repeatable")
    ap.add_argument("--koine-dir", default=".koine")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--title", default="")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--demo", action="store_true",
                    help="serve a throwaway sample store in a temp dir")
    args = ap.parse_args(argv)

    secret = os.environ.get("KOINE_WEBHOOK_SECRET", "")

    if args.demo:
        import tempfile
        root = tempfile.mkdtemp(prefix="koine-demo-")
        configs = dash.build_demo(root)
        print(f"koine service: demo store built in {root}")
        serve(host=args.host, port=args.port, configs=configs,
              repo_root=root, secret=secret)
        return 0

    if not args.source or not args.translation:
        ap.error("--source and at least one --translation are required "
                 "(or use --demo)")
    translations = dict(t.split("=", 1) for t in args.translation)
    cfg = dash.DashboardConfig(source=args.source, translations=translations,
                               koine_dir=args.koine_dir, title=args.title)
    serve(host=args.host, port=args.port, configs=[cfg],
          repo_root=args.repo_root, secret=secret)
    return 0


if __name__ == "__main__":
    sys.exit(main())
