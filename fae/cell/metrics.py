"""THE reader of a cell's metrics.json for everything that computes with it.

A verifier writes the file; validation and scoring read it. The count fields
the engine computes with are numbers, and a verifier that writes one as the
text it parsed (`"28114"`) is normalised here, once: a string that is not a
number stays as it is, for the rules to judge.
"""
from __future__ import annotations

import json
from pathlib import Path

NUMERIC = ("load_total", "load_errors", "e2e_pass", "e2e_total",
           "ceiling_qps", "mount_delay_s")


def _number(v):
    if not isinstance(v, str):
        return v
    for cast in (int, float):
        try:
            return cast(v.strip())
        except ValueError:
            pass
    return v


def normalised(doc: dict) -> dict:
    return {k: (_number(v) if k in NUMERIC else v) for k, v in doc.items()}


def read(ws) -> dict:
    """The cell's metrics, or {} when the file is missing or not JSON."""
    try:
        doc = json.loads((Path(ws) / "metrics.json").read_text())
    except (OSError, ValueError):
        return {}
    return normalised(doc) if isinstance(doc, dict) else {}
