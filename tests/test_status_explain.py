"""`koine status --explain` must not mislabel an integrity failure.

The explain verdict has to match the gate's exit-code severity: a TAMPERED or
UNSOURCED block is exit 2 (BROKEN) even when the ledger chain itself is intact.
An earlier version only checked the chain and printed DRIFT over a tampered
translation; this pins the fix.
"""
from koine.__main__ import main
from koine.dashboard import build_demo


def test_explain_reports_broken_when_a_block_is_tampered(tmp_path, capsys):
    build_demo(tmp_path)  # the es store carries a TAMPERED and an UNSOURCED block
    rc = main([
        "--koine-dir", str(tmp_path / ".koine"),
        "status", "--explain",
        "--source", str(tmp_path / "README.md"),
        "--translation", f"es={tmp_path / 'README.es.md'}",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gate: BROKEN" in out          # integrity failure, not DRIFT
    assert "integrity hits:" in out       # and it names the offending blocks


def test_explain_reports_drift_when_only_stale_or_untranslated(tmp_path, capsys):
    build_demo(tmp_path)  # ru drifted only (one UNTRANSLATED), chain intact
    main([
        "--koine-dir", str(tmp_path / ".koine"),
        "status", "--explain",
        "--source", str(tmp_path / "README.md"),
        "--translation", f"ru={tmp_path / 'README.ru.md'}",
    ])
    out = capsys.readouterr().out
    assert "gate: DRIFT" in out
    assert "gate: BROKEN" not in out
