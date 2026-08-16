"""Partitioned ledger store: one hash chain per language, one root manifest.

Why partitioned: translation PRs are per-language and concurrent. A single
ledger file makes every PR conflict with every other; per-language chains
keep each PR's writes isolated. The manifest is versioned from day one so
the on-disk shape can evolve without breaking repos that adopted earlier
(see versioned-schema-evolution: the format will outlive this code).

Layout inside a repo:
    .koine/manifest.json          {"schema_version": 1, "ledgers": {lang: relpath}}
    .koine/ledger.<lang>.jsonl    per-language hash chain (own genesis)
    .koine/glossary.json          shared glossary
"""
from __future__ import annotations

import json
from pathlib import Path

from .ledger import Ledger

SCHEMA_VERSION = 1
MANIFEST = "manifest.json"


class UnsupportedSchema(Exception):
    pass


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST

    def _read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {"schema_version": SCHEMA_VERSION, "ledgers": {}}
        data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        v = data.get("schema_version")
        if v != SCHEMA_VERSION:
            raise UnsupportedSchema(
                f"manifest schema_version {v!r} not supported by this koine "
                f"(expected {SCHEMA_VERSION}); upgrade koine or migrate "
                f"explicitly — guessing would corrupt the chains"
            )
        return data

    def _write_manifest(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def languages(self) -> list[str]:
        return sorted(self._read_manifest()["ledgers"].keys())

    def ledger(self, lang: str) -> Ledger:
        """Ledger for *lang*, registering it in the manifest on first use."""
        data = self._read_manifest()
        rel = data["ledgers"].get(lang)
        if rel is None:
            rel = f"ledger.{lang}.jsonl"
            data["ledgers"][lang] = rel
            self._write_manifest(data)
        return Ledger(self.root / rel)

    def glossary_path(self) -> Path:
        return self.root / "glossary.json"
