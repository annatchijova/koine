"""Dashboard tests. The data assembly (`build_snapshot`) is a pure function of
the on-disk store, exercised directly here the same way `handle_push` is in
test_webhook; the HTTP layer only serializes it.
"""
import json

from koine.dashboard import (
    DashboardConfig,
    GATE_BROKEN,
    GATE_DRIFT,
    GATE_OK,
    build_demo,
    build_snapshot,
    make_handler,
)
from koine.ledger import Ledger

# reuse the same source shape the webhook tests use
SRC = """# annaconda

Run the tests with `PYTHONPATH=$(pwd) python3 -m pytest` before deploying.

```bash
gcloud run deploy vigia-live --region us-central1
```

Set GEMINI_API_KEY and use the --verbose flag. Docs at https://example.org/x.
"""


def _setup_repo(tmp_path):
    (tmp_path / "README.md").write_text(SRC, encoding="utf-8")
    (tmp_path / ".koine").mkdir()
    Ledger(tmp_path / ".koine" / "ledger.es.jsonl")
    return DashboardConfig(source="README.md", translations={"es": "README.es.md"})


# ---------- read-only guarantee ----------

def test_snapshot_does_not_write_to_the_store(tmp_path):
    cfg = _setup_repo(tmp_path)
    before = {p.name for p in (tmp_path / ".koine").iterdir()}
    build_snapshot([cfg], repo_root=tmp_path)
    after = {p.name for p in (tmp_path / ".koine").iterdir()}
    # rendering must not create a manifest or any other file
    assert before == after
    assert "manifest.json" not in after


# ---------- gate light mirrors the engine ----------

def test_untranslated_source_is_drift(tmp_path):
    cfg = _setup_repo(tmp_path)
    snap = build_snapshot([cfg], repo_root=tmp_path)
    lang = snap["docs"][0]["langs"][0]
    assert lang["gate"] == GATE_DRIFT
    assert snap["overall_gate"] == GATE_DRIFT
    assert lang["counts"].get("UNTRANSLATED", 0) >= 1
    # the code fence is never eligible for translation
    assert lang["counts"].get("NOT_TRANSLATABLE", 0) >= 1


def test_fence_never_enters_the_work_queue(tmp_path):
    cfg = _setup_repo(tmp_path)
    snap = build_snapshot([cfg], repo_root=tmp_path)
    lang = snap["docs"][0]["langs"][0]
    reasons = {(q["block_index"], q["reason"]) for q in lang["queue"]}
    assert (0, "UNTRANSLATED") in reasons
    assert not any(r == "NOT_TRANSLATABLE" for _, r in reasons)


# ---------- end-to-end over the demo store ----------

def test_demo_exhibits_every_interesting_state(tmp_path):
    configs = build_demo(tmp_path)
    snap = build_snapshot(configs, repo_root=tmp_path)
    assert snap["overall_gate"] == GATE_BROKEN  # es has TAMPERED + UNSOURCED

    langs = {l["lang"]: l for l in snap["docs"][0]["langs"]}
    es = langs["es"]
    seen = set(es["counts"])
    # the states the demo is built to surface
    for expected in ("TAMPERED", "STALE", "MACHINE_ONLY", "UNSOURCED",
                     "UNTRANSLATED", "NOT_TRANSLATABLE"):
        assert expected in seen, f"{expected} missing from es demo: {seen}"
    assert es["gate"] == GATE_BROKEN

    ru = langs["ru"]
    # ru translated everything except one block -> a clean drift, no integrity break
    assert ru["gate"] == GATE_DRIFT
    assert ru["chain"]["ok"] is True
    assert ru["counts"].get("CURRENT", 0) >= 1


def test_demo_chain_is_anchored_and_intact(tmp_path):
    configs = build_demo(tmp_path)
    snap = build_snapshot(configs, repo_root=tmp_path)
    for lang in snap["docs"][0]["langs"]:
        ch = lang["chain"]
        assert ch["linkage_ok"] and ch["integrity_ok"]
        assert ch["anchored"] is True
        assert ch["truncated"] is False
        assert ch["entries"] >= 1
        assert len(ch["head"]) == 64  # sha-256 hex


def test_translation_side_findings_carry_translation_side(tmp_path):
    configs = build_demo(tmp_path)
    snap = build_snapshot(configs, repo_root=tmp_path)
    es = {l["lang"]: l for l in snap["docs"][0]["langs"]}["es"]
    unsourced = [b for b in es["blocks"] if b["state"] == "UNSOURCED"]
    assert unsourced
    assert all(b["side"] == "translation" for b in unsourced)


# ---------- HTTP layer ----------

class _FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b


def _get(handler_cls, path):
    """Drive the handler's do_GET without a socket, capturing the response."""
    h = handler_cls.__new__(handler_cls)
    h.path = path
    h.captured = {}
    h.wfile = _FakeWriter()

    def send_response(code):
        h.captured["code"] = code

    def send_header(k, v):
        h.captured.setdefault("headers", {})[k] = v

    def end_headers():
        pass

    h.send_response = send_response
    h.send_header = send_header
    h.end_headers = end_headers
    h.do_GET()
    return h.captured, h.wfile.data


def test_http_serves_page_and_snapshot(tmp_path):
    configs = build_demo(tmp_path)
    handler_cls = make_handler(configs, tmp_path)

    cap, body = _get(handler_cls, "/")
    assert cap["code"] == 200
    assert cap["headers"]["Content-Type"].startswith("text/html")
    assert b"koine" in body

    cap, body = _get(handler_cls, "/api/snapshot")
    assert cap["code"] == 200
    assert cap["headers"]["Content-Type"] == "application/json"
    snap = json.loads(body)
    assert snap["overall_gate"] in (GATE_OK, GATE_DRIFT, GATE_BROKEN)
    assert snap["docs"][0]["langs"]

    cap, body = _get(handler_cls, "/healthz")
    assert cap["code"] == 200
    assert json.loads(body) == {"status": "ok"}

    cap, body = _get(handler_cls, "/nope")
    assert cap["code"] == 404
