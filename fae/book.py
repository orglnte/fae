"""A book: one JSON file of shared state, read whole and replaced whole."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _nothing():
    yield


class Book:
    """One JSON file. A missing or unreadable file reads as `default`; a
    save replaces the file whole, under `changing()` when one is given."""

    def __init__(self, path, default=dict, changing=None):
        self.path = Path(path)
        self._default = default
        self._changing = changing

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self._default()

    def save(self, doc):
        with (self._changing() if self._changing else _nothing()):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(doc, sort_keys=True))
            os.replace(tmp, self.path)
        return doc
