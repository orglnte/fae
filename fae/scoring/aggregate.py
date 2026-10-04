#!/usr/bin/env python3
"""Aggregate all scored cells into the analyst hand-off files.

Reads workspaces.nosync/*/score.json (produced by score_cell.py) and writes:
    workspaces.nosync/results.csv   — one row per cell (flat; open in any tool)
    workspaces.nosync/results.json  — the same rows + a summary block with the
                            metrics per model and variant, the failure-class
                            distribution, and what the experiment adds.

Stdlib only. Usage:  python3 fae/scoring/aggregate.py
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from math import comb
from pathlib import Path

from fae import paths as _paths  # noqa: E402

REPO_ROOT = _paths.ROOT
WORKSPACES = next((REPO_ROOT / n for n in ("workspaces.nosync", "workspaces")
                   if (REPO_ROOT / n).is_dir()), REPO_ROOT / "workspaces.nosync")
OUT_CSV = WORKSPACES / "results.csv"
OUT_JSON = WORKSPACES / "results.json"

# Width of the LoC mean sub-field, so the -min/+max offsets that follow it start
# at the same character on every row. 4 = the largest mean the corpus produces
# (~1700 lines); widen it if a variant ever exceeds 9999.
LOC_MEAN_W = 4

CSV_COLUMNS = [
    "cell_id", "model", "task", "variant", "factors", "impl", "repeat",
    "first_pass_correct", "correctness_tier",
    "deploy_ok", "e2e_pass", "e2e_total", "e2e_green",
    "load_errors", "load_total", "k6_available", "verify_stage_failed",
    "iterations_to_green", "green", "revoked", "budget_exhausted",
    "agent_s_total", "agent_s_per_attempt",
    "files", "language_count", "lines", "sloc", "total_lines", "grader", "grading_missing",
]


def short_model(name: str) -> str:
    """Compact a AGENT_MODEL for DISPLAY only.

        Gemini 3.1 Pro (High)     -> gemini-3.1p
        claude-haiku-4-5-20251001 -> haiku-4.5
        claude-sonnet-5           -> sonnet-5

    The full identifier stays in results.csv and results.json — this shortens
    the printed table, never the data. A 30-char column existed to fit one
    25-char worst case whose bulk is a vendor prefix and a build date that
    never vary within a model.

    Rule-based rather than a lookup dict on purpose: an unreleased model must
    render sensibly without anyone remembering to add it, and a stale dict
    would silently print the wrong short name for a new build.
    """
    if name in POOLED_LABELS:
        return name
    s = re.sub(r"\s*\([^)]*\)", "", name or "").strip().lower()  # drop "(High)"
    s = s.rsplit("/", 1)[-1]               # gateway/provider path: keep the model
    s = re.sub(r"-\d{8}$", "", s)          # trailing build date
    s = re.sub(r"^claude-", "", s)         # vendor prefix carries nothing here
    s = s.replace(" ", "-")
    s = re.sub(r"-pro\b", "p", s)          # gemini-3.1-pro -> gemini-3.1p
    s = re.sub(r"-flash\b", "f", s)
    s = re.sub(r"(\d)-(\d)(?!\d)", r"\1.\2", s)   # haiku-4-5 -> haiku-4.5
    return s or (name or "?")


def mean(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 4) if nums else None


def rate(values: list) -> float | None:
    """Fraction of True among non-None booleans."""
    bools = [bool(v) for v in values if v is not None]
    return round(sum(bools) / len(bools), 4) if bools else None


def _cell(ws: Path):
    from fae.cell.cell import Cell
    return Cell(ws.name, workspaces=ws.parent)


def stale_records() -> list[tuple[str, str]]:
    """(cell, newer_source) for every score.json older than one of its inputs
    (the cell's env, ledger or metrics). aggregate only globs the records, so
    a table built from records that predate a scoring-rule change looks
    identical to a correct one; mtime is a coarse signal but a free one."""
    out = []
    if not WORKSPACES.is_dir():
        return out
    for p in sorted(WORKSPACES.glob("*/score.json")):
        rec_mtime = p.stat().st_mtime_ns
        for src, mt in _cell(p.parent).mtimes().items():
            if mt is not None and mt > rec_mtime:
                out.append((p.parent.name, src))
                break
    return out


def load_cells(include_tainted: bool = False) -> tuple[list[dict], list[tuple[str, str]]]:
    """Every scored cell, minus the TAINTED ones unless asked: a cell the
    validator distrusts must not move a mean silently. Returns (cells,
    excluded) where excluded is (cell, first taint) for the caller to print."""
    cells, excluded = [], []
    if WORKSPACES.is_dir():
        for p in sorted(WORKSPACES.glob("*/score.json")):
            c = json.loads(p.read_text())
            if "impl" not in c:
                c["impl"] = impl_of(p.parent)
            taint = first_taint(p.parent)
            if taint is not None and not include_tainted:
                excluded.append((p.parent.name, taint))
                continue
            cells.append(c)
    return cells, excluded


def first_taint(ws: Path) -> str | None:
    """The validator's verdict for the cell: None when VALID (or never
    validated), else the first taint's text."""
    try:
        v = json.loads(_cell(ws).read_derived("validation.json") or "{}")
    except json.JSONDecodeError:
        return None
    if v.get("verdict") != "TAINTED":
        return None
    taints = v.get("taints") or ["(no reason recorded)"]
    return taints[0]


PREVIOUS_IMPL = {"fae": "py", "py": "bash"}


def impl_of(ws: Path) -> str:
    """The cell driver from cell.env, for a score.json written before the
    record carried it."""
    return _cell(ws).impl or "bash"


def filter_cells(cells: list[dict], variant: str | None = None,
                 impl: str | None = None, where: dict | None = None) -> tuple[list[dict], list[str]]:
    """The scoreboard's cuts, each announced: a filtered table must never be
    mistaken for the full corpus. `where` is {factor: level}."""
    banners = []
    cuts = [("variant", variant, lambda c, w: c.get("variant") == w),
            ("impl", impl, lambda c, w: c.get("impl") == w)]
    cuts += [(f"factor {k}", v, lambda c, w, k=k: (c.get("factors") or {}).get(k) == w)
             for k, v in sorted((where or {}).items())]
    for label, want, keep in cuts:
        if want is None:
            continue
        total = len(cells)
        cells = [c for c in cells if keep(c, want)]
        banners.append(f"FILTERED: {label} '{want}' only — showing "
                       f"{len(cells)} of {total} scored cell(s)")
    return cells, banners


def factors_text(factors) -> str:
    return ";".join(f"{k}={v}" for k, v in sorted((factors or {}).items()))


def flat_row(c: dict) -> dict:
    surface = c.get("author_surface", {}) or {}
    return {
        "cell_id": c.get("cell_id"),
        "model": c.get("model"),
        "task": c.get("task"),
        "variant": c.get("variant"),
        "factors": factors_text(c.get("factors")),
        "impl": c.get("impl"),
        "repeat": c.get("repeat"),
        "first_pass_correct": c.get("first_pass_correct"),
        "correctness_tier": c.get("correctness_tier"),
        "deploy_ok": c.get("deploy_ok"),
        "e2e_pass": c.get("e2e_pass"),
        "e2e_total": c.get("e2e_total"),
        "e2e_green": c.get("e2e_green"),
        "load_errors": c.get("load_errors"),
        "load_total": c.get("load_total"),
        "k6_available": c.get("k6_available"),
        "verify_stage_failed": c.get("verify_stage_failed"),
        "iterations_to_green": c.get("iterations_to_green"),
        "green": c.get("green"),
        "budget_exhausted": c.get("budget_exhausted"),
        "agent_s_total": c.get("agent_s_total"),
        "agent_s_per_attempt": c.get("agent_s_per_attempt"),
        "files": surface.get("files"),
        "language_count": surface.get("language_count"),
        "lines": surface.get("lines"),
        "sloc": surface.get("sloc"),
        "total_lines": surface.get("total_lines"),
        "grader": c.get("grader"),
        "grading_missing": c.get("grading_missing"),
    }


def _e2e_pass_rate(c: dict) -> float | None:
    total = c.get("e2e_total")
    passed = c.get("e2e_pass")
    if isinstance(total, int) and total > 0 and isinstance(passed, int):
        return passed / total
    return None


def _load_error_rate(cells: list[dict]) -> float | None:
    """Aggregate load-error rate over the e2e-GREEN builds that ran k6
    (load is only measured on green builds)."""
    errs = 0
    tot = 0
    for c in cells:
        if c.get("e2e_green") and isinstance(c.get("load_total"), int):
            tot += c["load_total"]
            errs += c.get("load_errors") or 0
    return round(errs / tot, 4) if tot > 0 else None


def min_val(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return float(min(nums)) if nums else None


def max_val(values: list) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    return float(max(nums)) if nums else None


def cell_metrics(cells: list[dict]) -> dict:
    return {
        "n_cells": len(cells),
        # The raw green count, not just green_rate: a significance test on
        # the bash-vs-py comparison needs the actual 2x2 table, and
        # reconstructing it from a rounded rate is a needless way to get
        # a wrong count back.
        "n_green": sum(1 for c in cells if c.get("green")),
        "first_pass_correct_rate": rate([c.get("first_pass_correct") for c in cells]),
        # AUTO (verify.sh): live functional + load outcomes
        "e2e_green_rate": rate([c.get("e2e_green") for c in cells]),
        "green_rate": rate([c.get("green") for c in cells]),
        # went green, then the 6-shape gate revoked it: its own outcome —
        # folding it into green overstates success, into censored overstates
        # failure (a revoked cell DID solve the task once)
        "revoked_rate": rate([bool(c.get("revoked")) for c in cells]),
        "mean_e2e_pass_rate": mean([_e2e_pass_rate(c) for c in cells]),
        "load_error_rate_on_green": _load_error_rate(cells),
        # metric 3 + censoring
        "min_iterations_to_green": min_val([c.get("iterations_to_green") for c in cells]),
        "mean_iterations_to_green": mean([c.get("iterations_to_green") for c in cells]),
        "max_iterations_to_green": max_val([c.get("iterations_to_green") for c in cells]),
        "budget_censored_rate": rate([bool(c.get("budget_exhausted")) for c in cells]),
        # authoring time: to green over GREEN cells (as ITG), per attempt over all
        "min_agent_s_to_green": min_val([c.get("agent_s_total") for c in cells if c.get("green")]),
        "mean_agent_s_to_green": mean([c.get("agent_s_total") for c in cells if c.get("green")]),
        "max_agent_s_to_green": max_val([c.get("agent_s_total") for c in cells if c.get("green")]),
        "mean_agent_s_per_attempt": mean([c.get("agent_s_per_attempt") for c in cells]),
        # metric 4 (agent-authored surface)
        # GREEN CELLS ONLY: a non-green cell's files are whatever its last failed
        # attempt left, possibly mid-edit, so counting them would measure how far
        # an arm got before giving up rather than how much code the task takes,
        # on a different population from ITG.
        "n_green_surface": sum(1 for c in cells
                               if c.get("green")
                               and (c.get("author_surface") or {}).get("lines") is not None),
        "mean_files": mean([(c.get("author_surface") or {}).get("files")
                            for c in cells if c.get("green")]),
        "mean_languages": mean([(c.get("author_surface") or {}).get("language_count")
                                for c in cells if c.get("green")]),
        # Spread, not just centre: a mean alone cannot distinguish an arm whose
        # cells all converge on one size from one that ranges 3x, and the two
        # say different things about how constrained the task was.
        "min_lines": min_val([(c.get("author_surface") or {}).get("lines")
                              for c in cells if c.get("green")]),
        "mean_lines": mean([(c.get("author_surface") or {}).get("lines")
                            for c in cells if c.get("green")]),
        "max_lines": max_val([(c.get("author_surface") or {}).get("lines")
                              for c in cells if c.get("green")]),
        # Logic-only condition of the same green-only population: non-blank,
        # non-comment lines. Not printed; comment-heavy models inflate raw
        # lines and this is the number to compare across models.
        "min_sloc": min_val([(c.get("author_surface") or {}).get("sloc")
                             for c in cells if c.get("green")]),
        "mean_sloc": mean([(c.get("author_surface") or {}).get("sloc")
                           for c in cells if c.get("green")]),
        "max_sloc": max_val([(c.get("author_surface") or {}).get("sloc")
                             for c in cells if c.get("green")]),
        # Not printed. "How much code does a delivered solution take" (mean_lines,
        # green-only) and "what did this arm cost in authored code, failures
        # included" are different questions; the second was the original reading
        # of this metric and is kept rather than dropped.
        "mean_lines_all_cells": mean([(c.get("author_surface") or {}).get("lines")
                                      for c in cells]),
    }


def format_loc(mean_lines, min_lines, max_lines) -> str:
    """One LoC cell: the mean, then its distance to each end of the row.

    The mean leads because it is the only figure comparable across rows, and it
    is right-aligned in a fixed field so the offsets start at the same character
    everywhere — they are read DOWN the column, and a mean that grows a digit
    would otherwise shift them out of line.

    The offsets stay asymmetric rather than collapsing to one +/- interval: a
    row can reach 128 below its mean and 341 above, and a symmetric interval
    would describe a distribution that is not there.

    Extracted from main() so the alignment can be tested — it is a presentation
    rule a reader depends on, which makes it worth pinning rather than inline.
    """
    if mean_lines is None:
        return f"{'-':>{LOC_MEAN_W}}"
    lo = mean_lines - (min_lines if min_lines is not None else mean_lines)
    hi = (max_lines if max_lines is not None else mean_lines) - mean_lines
    return f"{mean_lines:>{LOC_MEAN_W}.0f}   -{lo:.0f}/+{hi:.0f}"


def delta(a: float | None, b: float | None) -> float | None:
    """a - b, guarding None."""
    if a is None or b is None:
        return None
    return round(a - b, 4)


def fisher_exact_p(a: int, b: int, c: int, d: int) -> float | None:
    """Two-sided Fisher's exact test on the 2x2 table
        [[a, b],
         [c, d]]
    (a = cut green, b = cut non-green, c = baseline green, d = baseline
    non-green). Exact hypergeometric summation — every arm in this corpus
    is a handful of cells, far too few for the normal approximation a
    chi-square or z-test on proportions relies on to hold. No scipy: this
    file is stdlib only, and the table is always small enough that summing
    the exact distribution costs nothing.

    None when either row or column total is 0 — there is no comparison to
    make when one side never ran."""
    row1, row2 = a + b, c + d
    col1, col2 = a + c, b + d
    total = row1 + row2
    if 0 in (row1, row2, col1, col2):
        return None

    def p_k(k: int) -> float:
        return comb(row1, k) * comb(row2, col1 - k) / comb(total, col1)

    lo, hi = max(0, col1 - row2), min(row1, col1)
    p_obs = p_k(a)
    # Every table at least as extreme as the observed one, either
    # direction: "two-sided" here means "no more likely than what we saw",
    # not "same sign of deviation" — the standard definition for this test.
    return sum(p_k(k) for k in range(lo, hi + 1) if p_k(k) <= p_obs * (1 + 1e-7))


def format_significance(p: float | None) -> str:
    """'-' with nothing to test, else the p-value with a threshold marker
    (* < .05, ** < .01) — the number itself travels with the marker so a
    reader is never asked to trust a bare asterisk."""
    if p is None:
        return "-"
    marker = "**" if p < 0.01 else "*" if p < 0.05 else ""
    val = "<.01" if p < 0.01 else f"{p:.2f}"
    return f"p{val}{marker}"


def significance_grade(p: float | None) -> str:
    """The plain-English read of the same p-value, next to it rather than
    instead of it: 'p1.00' is easy to misread at a glance as '100% sure',
    which is the opposite of what it means. A word next to the number
    cannot be misread that way."""
    if p is None:
        return "-"
    if p < 0.01:
        return "strong"
    if p < 0.05:
        return "significant"
    return "not sig"


def baseline_compare(by_mv: dict, base_mv: dict) -> dict:
    """Per row of the cut table, the same row in the baseline population:
    its n, green rate and mean ITG, the cut minus the baseline, and a
    Fisher's exact p-value on the green/non-green 2x2 table. A row with no
    baseline compares to nothing."""
    out = {}
    for key, m in by_mv.items():
        b = base_mv.get(key)
        if not b:
            out[key] = None
            continue
        cut_n, cut_g = m.get("n_cells", 0), m.get("n_green", 0)
        base_n, base_g = b.get("n_cells", 0), b.get("n_green", 0)
        out[key] = {
            "n": base_n,
            "green_rate": b.get("green_rate"),
            "d_green": delta(m.get("green_rate"), b.get("green_rate")),
            "mean_itg": b.get("mean_iterations_to_green"),
            "d_itg": delta(m.get("mean_iterations_to_green"),
                           b.get("mean_iterations_to_green")),
            "fisher_p": fisher_exact_p(cut_g, cut_n - cut_g, base_g, base_n - base_g),
        }
    return out


def format_compare(b: dict | None) -> tuple[str, str, str]:
    """(N: %GRN  Δpt, ITG  Δ, SIG): the baseline's own n leads the
    green-rate field — a rate with no denominator in sight reads as more
    certain than it is — then the cut's green rate and mean ITG, each with
    its delta from the baseline in the same field, then the significance
    test as one field: the raw p-value and its plain-English grade
    together, since a reader who wants either wants both right next to
    each other, not in separate columns to scan between. Three columns,
    not the eight the numbers could in principle fill, and not one string
    packing all of them where a reader would have to parse the separators
    by eye."""
    if not b:
        return ("-", "-", "-")
    gr = f"{b['n']}: %{b['green_rate'] * 100:.0f}" if b["green_rate"] is not None else f"{b['n']}: -"
    dg = f"{b['d_green'] * 100:+.0f}" if b["d_green"] is not None else ""
    grn = f"{gr}  {dg}" if dg else gr
    itg = f"{b['mean_itg']:.1f}" if b["mean_itg"] is not None else "-"
    di = f"{b['d_itg']:+.1f}" if b["d_itg"] is not None else ""
    itgf = f"{itg}  {di}" if di else itg
    p = b.get("fisher_p")
    sig = f"{format_significance(p)}  {significance_grade(p)}" if p is not None else "-"
    return (grn, itgf, sig)


def order_rows(by_mv: dict, compare: dict | None, sort_discrepancy: bool = False,
               sort_significant: bool = False) -> list[tuple[str, dict]]:
    """The rows to print, in print order. Default is group_and_rank's own
    order (best green first, within each model).

    --sort-discrepancy alone ranks by how far a row's cut diverged from
    its baseline, biggest gap first. --sort-significant alone ranks
    significant rows (Fisher's p < .05) first, most significant first.
    Both together put the rows that are BOTH significant AND big — the
    ones worth a reader's attention — strictly on top, discrepancy only
    breaking ties within the significant/not-significant split; the
    Fisher's p-value stops mattering as a sort key once significance is
    combined with magnitude, because 'how sure' and 'how much' are
    answering different questions and a slightly-more-certain small gap
    should not outrank a decisively larger one.

    A row with no baseline sorts last under every mode; extracted so the
    ranking rules can be tested without a live corpus."""
    rows = list(by_mv.items())
    if compare is None or not (sort_discrepancy or sort_significant):
        return rows

    def b_of(kv):
        return compare.get(kv[0])

    def no_baseline(kv):
        return b_of(kv) is None

    def is_significant(kv):
        b = b_of(kv)
        p = b.get("fisher_p") if b else None
        return p is not None and p < 0.05

    def disc(kv):
        return discrepancy(b_of(kv)) or 0.0

    def p_value(kv):
        b = b_of(kv)
        p = b.get("fisher_p") if b else None
        return p if p is not None else float("inf")

    if sort_significant and sort_discrepancy:
        rows.sort(key=lambda kv: (no_baseline(kv), not is_significant(kv), -disc(kv)))
    elif sort_significant:
        rows.sort(key=lambda kv: (no_baseline(kv), not is_significant(kv), p_value(kv)))
    else:
        rows.sort(key=lambda kv: (discrepancy(b_of(kv)) is None, -disc(kv)))
    return rows


def discrepancy(b: dict | None) -> float | None:
    """One sortable number for '--sort-discrepancy': how far the cut's
    green rate moved from its baseline, in points. Falls back to the ITG
    delta when green rate could not be compared (neither side ever went
    green) so a row is not dropped from the ranking just because its
    discrepancy shows up in the other metric."""
    if not b:
        return None
    if b.get("d_green") is not None:
        return abs(b["d_green"])
    if b.get("d_itg") is not None:
        return abs(b["d_itg"])
    return None


def format_agent_time(m: dict) -> tuple[str, str]:
    """The MIN and MIN/ATT cells: agent seconds shown as minutes."""
    mean = m.get("mean_agent_s_to_green")
    total = "-" if mean is None else (f"{m['min_agent_s_to_green'] / 60:.0f} / {mean / 60:.1f} / "
                                      f"{m['max_agent_s_to_green'] / 60:.0f}")
    per = m.get("mean_agent_s_per_attempt")
    return total, "-" if per is None else f"{per / 60:.1f}"


def table_columns(vs: str | None) -> list[tuple[str, int]]:
    """(header, width) per column, in print order. The LoC column must fit
    format_loc's widest value or the columns after it walk left."""
    return ([("AGENT", 16), ("VARIANT", 24)]
            + [("REPS", 4), ("E2E", 5), ("GREEN", 5), ("ITG mn/avg/mx", 14),
               ("MIN mn/avg/mx", 17), ("MIN/ATT", 7),
               ("SLoC avg -mn/+mx", LOC_MEAN_W + len("   -9999/+9999"))]
            + ([("N: GRN%  Δpt", 15), ("ITG  Δ", 10), ("SIG(p)  GRADE", 19)]
               if vs else []))


def _definition():
    for p in (str(REPO_ROOT), str(_paths.ENGINE.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from fae import experiment as _experiment
    return _experiment.definition()


def _pooled_models():
    """Model id -> scoreboard row label, the experiment's declaration
    (POOLED_MODELS in its definition). Grouping and display only: every
    cell record keeps the exact id it ran under."""
    return _definition().pooled_models


def experiment_summary(cells, metrics):
    """The summary entries the experiment's definition adds (`report_summary`:
    its gaps between variants, its notes), computed with this module's per-group
    metrics and None-safe delta so its numbers are the table's."""
    return _definition().report_summary(cells, cell_metrics, delta, list(metrics))


POOLED_MODELS = _pooled_models()
POOLED_LABELS = frozenset(POOLED_MODELS.values())


def pooled_model(model_id):
    return POOLED_MODELS.get(model_id, model_id)


def group_and_rank(cells: list[dict]) -> dict:
    """cells -> {"<model> / <variant>": metrics}, ranked.

    Grouped by AGENT, then BEST FIRST within that model: green rate
    descending, then mean iterations-to-green ascending, so the contrast the
    experiment is about is the first row under each model. A variant with NO
    green cell has mean_itg None and sorts LAST within its model; treating
    None as zero would rank it best, which is backwards.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in cells:
        groups[(pooled_model(c.get("model", "unknown")), c.get("variant"))].append(c)

    scored = {}
    for key, cs in groups.items():
        scored[key] = cell_metrics(cs)

    def rank(kv):
        (mod, var), m = kv
        gr = m.get("green_rate")
        itg = m.get("mean_iterations_to_green")
        return (str(mod),
                -(gr if gr is not None else 0.0),          # best green first
                itg if itg is not None else float("inf"),  # then fastest
                str(var))                                  # stable tie-break

    return {f"{mod} / {var}": m for (mod, var), m in sorted(scored.items(), key=rank)}


def taint_report(excluded, kept, details):
    """The excluded cells, as the scoreboard names them: by count with the
    flag that lists them, or the list itself under --tainted-cells-details."""
    if not excluded:
        return []
    if details:
        return ([f"{len(excluded)} TAINTED cell(s) (validator verdict; excluded from "
                 f"the scoreboard unless --include-tainted):"]
                + [f"  {cell}: {why[:110]}" for cell, why in excluded])
    return [f"FILTERED: tainted cells excluded — showing {kept} of "
            f"{kept + len(excluded)} scored cell(s) (--include-tainted to keep them)"]


def main() -> int:
    if "--tainted-cells-details" in sys.argv:
        cells, excluded = load_cells()
        for line in taint_report(excluded, len(cells), details=True) or ["no TAINTED cell"]:
            print(line)
        return 0
    # REFUSE rather than warn. A stale scoreboard is indistinguishable from a
    # correct one, so a warning just relocates the bug to whoever is reading
    # the scroll — which is how both of 2026-07-29's scoring fixes initially
    # appeared to have "no effect". Exiting non-zero makes publishing a stale
    # table impossible; --allow-stale is there for a deliberate quick look.
    stale = stale_records()
    if stale and "--allow-stale" not in sys.argv:
        print(f"REFUSING: {len(stale)} cell(s) have a score.json older than "
              f"their own inputs — the table would be built from records that "
              f"predate the data.", file=sys.stderr)
        for cell, src in stale[:10]:
            print(f"  {cell}: {src} is newer than score.json", file=sys.stderr)
        if len(stale) > 10:
            print(f"  ... and {len(stale) - 10} more", file=sys.stderr)
        print("\nFix: python3 cli.py results score      (re-scores, then aggregates)\n"
              "Override: python3 fae/scoring/aggregate.py --allow-stale",
              file=sys.stderr)
        return 2

    include_tainted = "--include-tainted" in sys.argv
    cells, excluded = load_cells(include_tainted)
    for line in taint_report(excluded, len(cells), details=False):
        print(line)
    if not excluded and include_tainted:
        print("NOTE: --include-tainted given; no tainted cell was found")
    # --variant ID restricts the whole scoreboard (table + CSV) to one
    # variant, --where FACTOR=LEVEL (repeatable) to the variants that are that
    # level of a factor, --impl to one cell driver.
    variant = impl = None
    where = {}
    if "--variant" in sys.argv:
        variant = sys.argv[sys.argv.index("--variant") + 1]
    if "--impl" in sys.argv:
        impl = sys.argv[sys.argv.index("--impl") + 1]
    for i, a in enumerate(sys.argv):
        if a == "--where" and i + 1 < len(sys.argv) and "=" in sys.argv[i + 1]:
            k, v = sys.argv[i + 1].split("=", 1)
            where[k] = v
    all_cells = cells
    cells, banners = filter_cells(cells, variant, impl, where)
    for b in banners:
        print(b)
    rows = [flat_row(c) for c in cells]
    # --impl cuts to one driver; its predecessor's cells under the same cuts
    # are the baseline every row is compared against.
    compare, other = None, None
    if impl:
        other = PREVIOUS_IMPL.get(impl, "py")
        base, _ = filter_cells(all_cells, variant, other, where)
        compare = baseline_compare(group_and_rank(cells), group_and_rank(base))

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    by_mv = group_and_rank(cells)

    metrics = ["first_pass_correct_rate",
               "e2e_green_rate", "green_rate", "revoked_rate",
               "mean_e2e_pass_rate", "load_error_rate_on_green",
               "mean_iterations_to_green", "budget_censored_rate",
               "mean_agent_s_to_green", "mean_agent_s_per_attempt",
               "mean_files", "mean_languages", "mean_lines"]

    summary = {
        "total_cells": len(cells),
        "cells_missing_grading": sum(1 for c in cells if c.get("grading_missing")),
        "by_model_variant": by_mv,
        "baseline": ({"impl": other, "delta_by_model_variant": compare}
                     if compare is not None else None),
        # the gaps between variants and the reading notes are the experiment's
        **experiment_summary(cells, metrics),
    }

    OUT_JSON.write_text(json.dumps({"cells": rows, "summary": summary}, indent=2) + "\n")
    
    print(f"\n[OK] Wrote {len(rows)} cell records to: {OUT_CSV}")
    print(f"[OK] Wrote aggregate summary to: {OUT_JSON}")
    
    cols = table_columns(other if compare is not None else None)
    width = sum(w for _, w in cols) + 3 * (len(cols) - 1)
    if compare is not None:
        print(f"\nVS baseline: impl={other}")
    sort_discrepancy = "--sort-discrepancy" in sys.argv
    sort_significant = "--sort-significant" in sys.argv
    if (sort_discrepancy or sort_significant) and compare is None:
        flag = "--sort-discrepancy" if sort_discrepancy else "--sort-significant"
        print(f"NOTE: {flag} has no effect without --impl "
              "(there is no baseline to diverge from)")
    rows_to_print = order_rows(by_mv, compare, sort_discrepancy, sort_significant)
    print("\n" + "=" * width)
    print(" | ".join(f"{h:<{w}}" for h, w in cols))
    print("-" * width)
    for key, m in rows_to_print:
        n = m.get("n_cells", 0)
        e2e = f"{m.get('mean_e2e_pass_rate', 0):.0%}" if m.get('mean_e2e_pass_rate') is not None else "-"
        grn = f"{m.get('green_rate', 0):.0%}" if m.get('green_rate') is not None else "-"
        
        itg_mean = m.get('mean_iterations_to_green')
        if itg_mean is not None:
            itg_min = m.get('min_iterations_to_green', 0)
            itg_max = m.get('max_iterations_to_green', 0)
            itg = f"{itg_min:.0f} / {itg_mean:.1f} / {itg_max:.0f}"
        else:
            itg = "-"
            
        # mean with its distance to each end, not the bare endpoints: the mean
        # is what a reader compares across rows, and the offsets say how far
        # the row's cells reach either side of it. Asymmetric on purpose — a
        # single outlier shows as one long arm rather than being averaged into
        # a symmetric interval it does not describe.
        lines = format_loc(m.get('mean_sloc'), m.get('min_sloc'), m.get('max_sloc'))
        agent_min, per_att = format_agent_time(m)
        
        mod, var = key.split(" / ", 1)
        vals = [short_model(mod), var] \
            + [str(n), e2e, grn, itg, agent_min, per_att, lines] \
            + (list(format_compare(compare.get(key))) if compare is not None else [])
        print(" | ".join(f"{v:<{w}}" for v, (_, w) in zip(vals, cols)))

    print("=" * width)
    print(" * ITG, SLoC: GREEN cells only -- denominator is GREEN, not REPS.")
    print("   A non-green cell never worked; its size is not comparable.")
    print(" * MIN = the agent's minutes up to green (charged attempts; GREEN cells only);")
    print("   MIN/ATT = its mean minutes per charged attempt, every cell. '-' when the")
    print("   ledger has no AGENT lines for those attempts.")
    print(" * SLoC = non-blank, non-comment lines authored by the agent, excluding the")
    print("   seeded skeleton. Raw line counts stay in results.csv/json.")
    if compare is not None:
        print(" * SIG(p) GRADE = two-sided Fisher's exact test on green vs non-green")
        print("   counts, cut against baseline: the p-value (* p<.05, ** p<.01), then the")
        print("   same result spelled out (not sig / significant / strong). '-' when")
        print("   either side has 0 cells.")
    print()
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
