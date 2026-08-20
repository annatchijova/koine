import hashlib
import hmac
import json

import pytest

from koine.agents.contracts import prepare_block, submit_candidate
from koine.glossary import Glossary
from koine.ledger import Ledger
from koine.webhook import (
    DocConfig,
    InvalidSignature,
    compute_work_queue,
    docs_affected,
    extract_changed_paths,
    handle_push,
    verify_signature,
)

SRC = """# annaconda

Run the tests with `PYTHONPATH=$(pwd) python3 -m pytest` before deploying.

```bash
gcloud run deploy vigia-live --region us-central1
```

Set GEMINI_API_KEY and use the --verbose flag. Docs at https://example.org/x.
"""

SECRET = "s3cr3t"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---------- signature verification ----------

def test_valid_signature_passes():
    body = b'{"a": 1}'
    verify_signature(SECRET, body, _sign(body))  # no raise


def test_wrong_secret_rejected():
    body = b'{"a": 1}'
    with pytest.raises(InvalidSignature):
        verify_signature(SECRET, body, _sign(body, secret="other"))


def test_tampered_body_rejected():
    body = b'{"a": 1}'
    sig = _sign(body)
    with pytest.raises(InvalidSignature):
        verify_signature(SECRET, b'{"a": 2}', sig)


def test_missing_header_rejected():
    with pytest.raises(InvalidSignature):
        verify_signature(SECRET, b"{}", None)


def test_malformed_header_rejected():
    with pytest.raises(InvalidSignature):
        verify_signature(SECRET, b"{}", "not-sha256-prefixed")


def test_empty_secret_refuses_even_with_matching_signature():
    body = b"{}"
    with pytest.raises(InvalidSignature):
        verify_signature("", body, _sign(body, secret=""))


# ---------- payload parsing ----------

def test_extract_changed_paths_unions_all_commits():
    payload = {
        "commits": [
            {"added": ["a.md"], "modified": ["README.md"], "removed": []},
            {"added": [], "modified": ["README.es.md"], "removed": ["old.md"]},
        ]
    }
    assert extract_changed_paths(payload) == {"a.md", "README.md", "README.es.md", "old.md"}


def test_extract_changed_paths_handles_no_commits():
    assert extract_changed_paths({}) == set()


def test_docs_affected_filters_by_tracked_paths():
    doc_a = DocConfig(source="README.md", translations={"es": "README.es.md"})
    doc_b = DocConfig(source="OTHER.md", translations={"es": "OTHER.es.md"})
    assert docs_affected([doc_a, doc_b], {"README.md"}) == [doc_a]
    assert docs_affected([doc_a, doc_b], {"unrelated.txt"}) == []


# ---------- end-to-end work queue derivation ----------

def _setup_repo(tmp_path):
    src = tmp_path / "README.md"
    src.write_text(SRC, encoding="utf-8")
    tr = tmp_path / "README.es.md"
    (tmp_path / ".koine").mkdir()
    led = Ledger(tmp_path / ".koine" / "ledger.es.jsonl")
    return src, tr, led


def test_work_queue_reports_untranslated_blocks(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    doc = DocConfig(source="README.md", translations={"es": "README.es.md"})
    items = compute_work_queue(doc, repo_root=tmp_path)
    reasons = {(i.block_index, i.reason) for i in items}
    assert (0, "UNTRANSLATED") in reasons
    assert (1, "UNTRANSLATED") in reasons
    assert not any(i.block_index == 2 for i in items)  # fence: NOT_TRANSLATABLE, never queued


def test_work_queue_empty_once_translated(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    for i in (0, 1, 3):
        prep = prepare_block(str(src), i)
        submit_candidate(source_path=str(src), block_index=i, lang="es",
                         translated_template=prep["template"], ledger=led,
                         glossary=Glossary(), reviewed=True, translation_file=str(tr))
    doc = DocConfig(source="README.md", translations={"es": "README.es.md"})
    assert compute_work_queue(doc, repo_root=tmp_path) == []


def test_handle_push_end_to_end(tmp_path):
    src, tr, led = _setup_repo(tmp_path)
    doc = DocConfig(source="README.md", translations={"es": "README.es.md"})
    payload = {"commits": [{"added": [], "modified": ["README.md"], "removed": []}]}
    body = json.dumps(payload).encode("utf-8")

    result = handle_push(secret=SECRET, signature_header=_sign(body), body=body,
                         docs=[doc], repo_root=tmp_path)
    assert result["changed_paths"] == ["README.md"]
    assert {(i["block_index"], i["reason"]) for i in result["work_queue"]} >= {
        (0, "UNTRANSLATED"), (1, "UNTRANSLATED"),
    }


def test_handle_push_rejects_bad_signature(tmp_path):
    doc = DocConfig(source="README.md", translations={"es": "README.es.md"})
    body = json.dumps({"commits": []}).encode("utf-8")
    with pytest.raises(InvalidSignature):
        handle_push(secret=SECRET, signature_header="sha256=deadbeef", body=body,
                    docs=[doc], repo_root=tmp_path)


def test_handle_push_skips_unaffected_docs(tmp_path):
    _setup_repo(tmp_path)
    doc = DocConfig(source="README.md", translations={"es": "README.es.md"})
    payload = {"commits": [{"added": [], "modified": ["unrelated.txt"], "removed": []}]}
    body = json.dumps(payload).encode("utf-8")
    result = handle_push(secret=SECRET, signature_header=_sign(body), body=body,
                         docs=[doc], repo_root=tmp_path)
    assert result["work_queue"] == []
