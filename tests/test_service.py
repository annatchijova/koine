"""Combined Cloud Run service: one server, dashboard GET + webhook POST. The
decision logic is already covered by test_dashboard and test_webhook; this
pins the routing and the fail-closed webhook behaviour behind one listener.
"""
import hashlib
import hmac
import json

from koine.dashboard import build_demo
from koine.service import _webhook_docs_from, make_handler

SECRET = "s3cr3t"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class _FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


class _FakeRfile:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n):
        return self._body[:n]


def _drive(handler_cls, method, path, *, headers=None, body=b""):
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.headers = headers or {}
    h.rfile = _FakeRfile(body)
    h.wfile = _FakeWriter()
    h.captured = {}

    h.send_response = lambda code: h.captured.__setitem__("code", code)
    h.send_header = lambda k, v: h.captured.setdefault("headers", {}).__setitem__(k, v)
    h.end_headers = lambda: None

    getattr(h, "do_" + method)()
    return h.captured, h.wfile.data


def test_webhook_docs_mirror_dashboard_configs(tmp_path):
    configs = build_demo(tmp_path)
    docs = _webhook_docs_from(configs)
    assert len(docs) == len(configs)
    assert docs[0].source == configs[0].source
    assert docs[0].translations == configs[0].translations


def test_get_routes_serve_dashboard(tmp_path):
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path, SECRET)

    cap, body = _drive(handler_cls, "GET", "/")
    assert cap["code"] == 200
    assert b"KOINE" in body

    cap, body = _drive(handler_cls, "GET", "/api/snapshot")
    assert cap["code"] == 200
    snap = json.loads(body)
    assert snap["docs"][0]["langs"]


def test_post_webhook_valid_push_returns_queue(tmp_path):
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path, SECRET)
    payload = {"commits": [{"added": [], "modified": ["README.md"], "removed": []}]}
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": "push",
               "X-Hub-Signature-256": _sign(body),
               "Content-Length": str(len(body))}
    cap, out = _drive(handler_cls, "POST", "/webhook", headers=headers, body=body)
    assert cap["code"] == 200
    result = json.loads(out)
    assert result["changed_paths"] == ["README.md"]
    # the demo README has pending work, so the queue is non-empty
    assert result["work_queue"]


def test_post_webhook_bad_signature_fails_closed(tmp_path):
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path, SECRET)
    body = json.dumps({"commits": []}).encode("utf-8")
    headers = {"X-GitHub-Event": "push",
               "X-Hub-Signature-256": "sha256=deadbeef",
               "Content-Length": str(len(body))}
    cap, out = _drive(handler_cls, "POST", "/webhook", headers=headers, body=body)
    assert cap["code"] == 401


def test_post_non_push_event_is_ignored(tmp_path):
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path, SECRET)
    headers = {"X-GitHub-Event": "ping", "Content-Length": "0"}
    cap, out = _drive(handler_cls, "POST", "/webhook", headers=headers, body=b"")
    assert cap["code"] == 202


def test_dashboard_serves_without_a_secret(tmp_path):
    # the read surface must not depend on a webhook secret being configured
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path, "")
    cap, body = _drive(handler_cls, "GET", "/api/snapshot")
    assert cap["code"] == 200
    # but the webhook fails closed when no secret is set
    payload = {"commits": []}
    b = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": _sign(b),
               "Content-Length": str(len(b))}
    cap, out = _drive(handler_cls, "POST", "/webhook", headers=headers, body=b)
    assert cap["code"] == 401
