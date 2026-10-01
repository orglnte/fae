#!/usr/bin/env python3
"""Inter-rater reliability for the LLM defect grading (rubric.md, DESIGN 7.6).

Every cell is graded TWICE by two independent judge invocations. This computes
how much they agree, because the primary metric (consistency_defect_count) is
judge-produced and a metric without a reliability figure is an opinion.

UNIT OF ANALYSIS: (cell x code category). For each cell and each category in
CAD1..CAD6, API1, POL1, each rater either did or did not assign that code to
that cell. That gives a 2x2 table per category and one pooled table overall —
the standard treatment for multi-label coding, and it answers the question
that matters ("do they agree on WHICH defect classes are present") rather than
just "do the totals match".

Cohen's kappa corrects raw agreement for agreement expected by chance:

    kappa = (po - pe) / (1 - pe)

Two known degeneracies are reported rather than hidden:
  - When a category is absent from both raters everywhere, po = 1 and pe = 1,
    so kappa is 0/0 — undefined, NOT perfect. Printed as "n/a (never coded)".
  - With heavy skew (a category almost always absent) kappa can be low despite
    high raw agreement — the kappa paradox. Raw agreement is printed alongside
    so the pair can be read together.

Usage:
    python3 fae/scoring/grader_agreement.py            # table
    python3 fae/scoring/grader_agreement.py --json     # machine-readable
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fae import paths as _paths  # noqa: E402

REPO_ROOT = _paths.ROOT
WORKSPACES = next((REPO_ROOT / n for n in ("workspaces.nosync", "workspaces")
                   if (REPO_ROOT / n).is_dir()), REPO_ROOT / "workspaces.nosync")

CATEGORIES = ["CAD1", "CAD2", "CAD3", "CAD4", "CAD5", "CAD6", "API1", "POL1"]


def _codes_of(entries) -> set[str]:
    """The set of code categories a rater assigned to a cell. Entries are
    objects ({"code": "CAD5", ...}); a bare string is tolerated."""
    out = set()
    for e in entries or []:
        c = e.get("code") if isinstance(e, dict) else e
        if c:
            out.add(str(c).strip().upper())
    return out


def kappa(pairs: list[tuple[bool, bool]]) -> tuple[float | None, float]:
    """Cohen's kappa and raw agreement for a list of (rater_a, rater_b) calls.
    Returns (None, po) when kappa is undefined (pe == 1)."""
    n = len(pairs)
    if not n:
        return None, 0.0
    both = sum(1 for a, b in pairs if a and b)
    neither = sum(1 for a, b in pairs if not a and not b)
    a_only = sum(1 for a, b in pairs if a and not b)
    b_only = sum(1 for a, b in pairs if not a and b)
    po = (both + neither) / n
    pa1, pb1 = (both + a_only) / n, (both + b_only) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if abs(1 - pe) < 1e-12:
        return None, po
    return (po - pe) / (1 - pe), po


def collect() -> tuple[list[dict], list[str]]:
    cells, skipped = [], []
    for p in sorted(WORKSPACES.glob("*/defects.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            skipped.append(f"{p.parent.name} (unreadable)")
            continue
        if d.get("codes") is None or d.get("codes_b") is None:
            skipped.append(f"{p.parent.name} (single-pass only)")
            continue
        cells.append({
            "cell": p.parent.name,
            "a": _codes_of(d["codes"]),
            "b": _codes_of(d["codes_b"]),
            "n_a": sum(1 for c in _codes_of(d["codes"]) if c.startswith("CAD")),
            "n_b": sum(1 for c in _codes_of(d["codes_b"]) if c.startswith("CAD")),
            "model_a": d.get("grader_model"),
            "model_b": d.get("grader_model_b"),
        })
    return cells, skipped


def main() -> int:
    cells, skipped = collect()
    as_json = "--json" in sys.argv

    if not cells:
        msg = ("no double-graded cell found — every defects.json is single-pass. "
               "Run: python3 cli.py results grade --judge-model claude-opus-5")
        print(json.dumps({"error": msg}) if as_json else f"ERROR: {msg}",
              file=sys.stderr)
        return 1

    per_cat = {}
    pooled: list[tuple[bool, bool]] = []
    for cat in CATEGORIES:
        pairs = [(cat in c["a"], cat in c["b"]) for c in cells]
        k, po = kappa(pairs)
        n_a = sum(1 for a, _ in pairs if a)
        n_b = sum(1 for _, b in pairs if b)
        per_cat[cat] = {"kappa": k, "raw_agreement": po, "n_a": n_a, "n_b": n_b}
        pooled += pairs
    k_all, po_all = kappa(pooled)

    exact = sum(1 for c in cells if c["n_a"] == c["n_b"])
    mad = sum(abs(c["n_a"] - c["n_b"]) for c in cells) / len(cells)

    models = sorted({m for c in cells for m in (c["model_a"], c["model_b"]) if m})
    out = {
        "cells_double_graded": len(cells),
        "cells_skipped": skipped,
        "grader_models": models,
        "same_model_both_passes": len(models) <= 1,
        "pooled": {"kappa": k_all, "raw_agreement": po_all},
        "by_category": per_cat,
        "consistency_defect_count": {
            "exact_match_cells": exact,
            "exact_match_rate": exact / len(cells),
            "mean_abs_difference": mad,
        },
    }
    if as_json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"\nInter-rater agreement — {len(cells)} double-graded cell(s)")
    print(f"graders: {', '.join(models) or 'unknown'}"
          + ("  (SAME model both passes — measures run-to-run stability, "
             "not cross-model validity)" if len(models) <= 1 else ""))
    if skipped:
        print(f"skipped: {len(skipped)} cell(s) not double-graded")
    print(f"\n{'CODE':<8} {'KAPPA':>8}  {'RAW AGR':>8}  {'A':>4} {'B':>4}")
    print("-" * 40)
    for cat in CATEGORIES:
        s = per_cat[cat]
        kv = "n/a" if s["kappa"] is None else f"{s['kappa']:.3f}"
        note = "  (never coded)" if s["n_a"] == 0 and s["n_b"] == 0 else ""
        print(f"{cat:<8} {kv:>8}  {s['raw_agreement']:>7.1%}  "
              f"{s['n_a']:>4} {s['n_b']:>4}{note}")
    print("-" * 40)
    kv = "n/a" if k_all is None else f"{k_all:.3f}"
    print(f"{'POOLED':<8} {kv:>8}  {po_all:>7.1%}")
    print(f"\nconsistency_defect_count: {exact}/{len(cells)} cells match exactly "
          f"({exact / len(cells):.0%}), mean |A-B| = {mad:.2f}")
    print("\nA/B columns = cells in which each rater assigned that code.")
    print("kappa n/a means undefined (pe=1), which is NOT perfect agreement.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
