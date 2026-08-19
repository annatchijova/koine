"""Glossary: the fleet's long-term memory about how terms must be rendered.

The glossary is a versioned JSON file living in the repo. Agents may PROPOSE
entries; only a human decision (recorded in the file itself) makes an entry
binding. Enforcement is mechanical: a candidate translation that renders a
binding term differently is rejected by the gate, not by a model's opinion.

Entry semantics:
  term            source-language term, matched case-sensitively
  renderings      {lang: required rendering}  — "@same" means keep verbatim
  status          "binding" | "proposed"
  decided_by      free-text provenance ("anna", "agent:glossary-steward")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

GLOSSARY_VERSION = 1
KEEP_VERBATIM = "@same"


@dataclass
class Entry:
    term: str
    renderings: dict
    status: str = "proposed"
    decided_by: str = ""


class Glossary:
    def __init__(self, entries: list[Entry] | None = None):
        self.entries = entries or []

    # ---------- persistence ----------
    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        p = Path(path)
        if not p.exists():
            return cls([])
        data = json.loads(p.read_text(encoding="utf-8"))
        if data.get("glossary_version") != GLOSSARY_VERSION:
            raise ValueError(
                f"glossary version {data.get('glossary_version')!r} not supported "
                f"(expected {GLOSSARY_VERSION}); migrate explicitly, do not guess"
            )
        return cls([Entry(**e) for e in data["entries"]])

    def save(self, path: str | Path) -> None:
        payload = {
            "glossary_version": GLOSSARY_VERSION,
            "entries": [asdict(e) for e in sorted(self.entries, key=lambda e: e.term)],
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ---------- mutation ----------
    def propose(self, term: str, renderings: dict, by: str) -> Entry:
        e = Entry(term=term, renderings=renderings, status="proposed", decided_by=by)
        self.entries.append(e)
        return e

    def bind(self, term: str, by: str) -> None:
        for e in self.entries:
            if e.term == term:
                e.status = "binding"
                e.decided_by = by
                return
        raise KeyError(term)

    # ---------- mechanical enforcement ----------
    def violations(self, source: str, translation: str, lang: str) -> list[str]:
        """Binding terms present in *source* whose required rendering for
        *lang* is absent from *translation*. Whole-word match on the source
        side to avoid substring false positives."""
        out = []
        for e in self.entries:
            if e.status != "binding":
                continue
            if not re.search(rf"(?<!\w){re.escape(e.term)}(?!\w)", source):
                continue
            required = e.renderings.get(lang)
            if required is None:
                continue
            needle = e.term if required == KEEP_VERBATIM else required
            if needle not in translation:
                out.append(f"{e.term!r} must appear as {needle!r} in {lang}")
        return out
