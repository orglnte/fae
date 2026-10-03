"""Post-DONE trust + the scoreboard: validate, score, aggregate.

`validate` decides whether to BELIEVE a verdict; `score` calls it as a
preamble (a scoreboard built on unexamined cells is worthless), which is why
the two live in one module rather than splitting validate out to sit beside
`fae/driver/validate.py`'s taint-rule engine (imported here as `taint` — the
rules; this module is the CLI-verb wrapper around them).
"""
from __future__ import annotations

import collections
import contextlib
import io
import subprocess
import sys

import ujson as json

from fae.cell.cell import Busy
from fae.driver import common
from fae.driver import render
from fae.driver.conduct import Conduct
from fae.driver import validate as taint


# --- post-DONE validation -----------------------------------------------------
# A verdict says WHAT happened; validation says whether to BELIEVE it. The two
# never merge: a failed run on an overloaded host is still failed, just
# untrustworthy — collapsing taint into the verdict would hide exactly the
# distinction the analysis needs ("N green, M failed, K excluded"). Verdicts
# stay green/failed/revoked/cancelled; validation writes $ws/validation.json
# and TAINTED cells are raised to the operator, never auto-requeued (a taint
# is a rig/host problem — requeuing would loop until the host got lucky).
# Mandatory-at-DONE: reconcile auto-validates any terminal cell that has no
# validation.json yet, so no verdict enters the dataset unexamined.


def validate(args, quiet=False):
    """Sweep DONE cells (or the selector's), apply the rules, report. TAINTED
    is raised here for the operator to act on — never auto-requeued.

    quiet=True prints a one-line summary instead of the per-cell table. The
    TAINTED count is never suppressed (it is what needs an operator decision);
    the per-cell taints are behind `score --tainted-cells-details`.
    `cli.py results validate` on its own is always verbose; only `score` (where the
    table is a preamble to the scoreboard) passes quiet, and --all-cells turns
    it back off."""
    rows, tainted = [], []
    # One docker-ps snapshot for the whole sweep: a per-cell call made this
    # loop O(cells) docker round-trips, and a DONE cell's container state
    # cannot change mid-sweep.
    boxes = Conduct.containers()
    for cid in common.select_cells(getattr(args, "selector", None) or "all"):
        ws = common.WS / cid
        st = Conduct.cell_state(ws, {}, boxes)
        if not st or st["state"] != "DONE" or st["why"] == "cancelled":
            continue
        try:
            doc = taint._validate_cell(ws)
        except Busy:
            rows.append((cid, f"{st['why']}", "HELD", "held by another process: not validated"))
            continue
        rows.append((cid, f"{st['why']}", doc["verdict"],
                     "; ".join(doc["taints"] + doc["warns"])[:60] or "-"))
        if doc["verdict"] == "TAINTED":
            tainted.append((cid, doc["taints"]))
    if not rows:
        print("no DONE cells match"); return
    if quiet:
        n_warn = sum(1 for r in rows if r[3] != "-" and r[2] == "VALID")
        by = collections.Counter(r[1] for r in rows)
        print(f"  {len(rows)} completed cell(s): "
              + ", ".join(f"{n} {k}" for k, n in sorted(by.items()))
              + f" | {len(rows) - len(tainted)} VALID, {len(tainted)} TAINTED"
              + (f", {n_warn} with warnings" if n_warn else ""))
    else:
        print(render.fmt_table(rows, ("CELL", "VERDICT", "VALIDATION", "NOTES")))
    if tainted and quiet:
        print(f"  {len(tainted)} TAINTED — `score --tainted-cells-details` lists them")
    elif tainted:
        print("\nTAINTED — operator decision needed (rerun? exclude? both are "
              "yours; nothing is auto-requeued):")
        for cid, ts in tainted:
            for t in ts:
                print(f"  {cid}: {t}")


def score(args):
    # Validate first. The per-cell VERDICT/VALIDATION list is one line per
    # terminal cell and now runs to 50+, which buries the aggregate table
    # underneath it — but it is also the only place a TAINTED cell is named,
    # so it can never be silently dropped: the count below always prints, and
    # anything tainted is always shown regardless of the flag.
    all_cells = getattr(args, "all_cells", False)
    single = getattr(args, "cell", None)
    if getattr(args, "tainted_cells_details", False):
        aggregate(args)
        return
    if not (all_cells or single):
        print("Validating cells before scoring... (per-cell list hidden, "
              "see --help for filters)")
    else:
        print("Validating cells before scoring...")
    sys.stdout.flush()
    args.selector = single
    validate(args, quiet=not (all_cells or single))
    print("\n--- Scoring --- (one record per cell: iterations-to-green, "
          "authored surface -> <cell>/score.json, which the scoreboard reads)")
    sys.stdout.flush()

    cids = [getattr(args, "cell")] if getattr(args, "cell", None) else common.select_cells("all")
    boxes = Conduct.containers()
    # This phase used to print NOTHING: 50+ subprocesses ran silently under a
    # bare header, so the only evidence they had worked was the aggregate table
    # appearing afterwards. A cell whose scoring failed left a traceback loose
    # in the scroll and no tally, making a partial scoreboard indistinguishable
    # from a complete one.
    # WS, not a second hardcoded workspaces.nosync: WS falls back to
    # plain workspaces/ when the .nosync tree is absent, so on a portable
    # checkout this returned None for every cid and printed
    # "scored 0 cell(s)" — then aggregate() still ran and produced a
    # scoreboard from stale score.json files.
    todo = []
    for cid in cids:
        st = Conduct.cell_state(common.WS / cid, {}, boxes)
        if st and st["state"] == "DONE" and st["why"] != "cancelled":
            todo.append(cid)

    # Progress bar on a TTY only: piped/logged runs must not fill the file
    # with \r frames. Errors clear the bar line first so they land on their
    # own line and survive in the scroll.
    bar_on = sys.stdout.isatty()

    def _bar(i, cid):
        if not bar_on:
            return
        n, w = len(todo), 24
        fill = int(w * i / n) if n else w
        line = f"\r  [{'█' * fill}{'░' * (w - fill)}] {i}/{n} {cid}"
        sys.stdout.write(line[:100].ljust(100))
        sys.stdout.flush()

    # In-process scoring: score_cell is imported ONCE and score_one() called
    # per cell. The old shape — subprocess.run(["python3", "fae/scoring/
    # score_cell.py", cid]) per cell — paid ~96ms of interpreter+import
    # startup 300+ times, 69% of a warm sweep. The isolation subprocess gave
    # for free (a crash in one cell can't kill the sweep) is kept by the
    # except below; stderr is captured per cell so failures report one line,
    # as the captured subprocess did.
    from fae.scoring import score_cell as _sc

    n_ok, failed = 0, []
    for i, cid in enumerate(todo):
        _bar(i, cid)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = _sc.score_one(cid, cell=common.cell(cid))
        except Exception as e:
            rc, _ = 1, err.write(f"{type(e).__name__}: {e}")
        if rc == 0:
            n_ok += 1
        else:
            failed.append(cid)
            tail = err.getvalue().strip().splitlines()
            if bar_on:
                sys.stdout.write("\r" + " " * 100 + "\r")
            print(f"  ERROR scoring {cid}: {tail[-1][:100] if tail else '(no output)'}")
    if bar_on:
        _bar(len(todo), "done")
        sys.stdout.write("\n")
    print(f"  scored {n_ok} cell(s) -> score.json"
          + (f", {len(failed)} FAILED" if failed else ""))

    if not getattr(args, "cell", None) and not getattr(args, "no_aggregate", False):
        print("\n--- Aggregate Scoreboard ---")
        sys.stdout.flush()
        aggregate(args)


def aggregate(args):
    argv = [sys.executable, "-m", "fae.scoring.aggregate"]
    if getattr(args, "allow_stale", False):
        argv.append("--allow-stale")
    if getattr(args, "variant", None):
        argv += ["--variant", args.variant]
    for w in getattr(args, "where", None) or []:
        argv += ["--where", w]
    if getattr(args, "impl", None):
        argv += ["--impl", args.impl]
    if getattr(args, "include_tainted", False):
        argv.append("--include-tainted")
    if getattr(args, "tainted_cells_details", False):
        argv.append("--tainted-cells-details")
    if getattr(args, "sort_discrepancy", False):
        argv.append("--sort-discrepancy")
    if getattr(args, "sort_significant", False):
        argv.append("--sort-significant")
    subprocess.run(argv, check=True)
