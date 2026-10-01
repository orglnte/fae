"""Post-DONE trust + the scoreboard + the defect scan: validate, score,
aggregate, grade.

`validate` decides whether to BELIEVE a verdict; `score` calls it as a
preamble (a scoreboard built on unexamined cells is worthless), which is why
the two live in one module rather than splitting validate out to sit beside
`fae/driver/validate.py`'s taint-rule engine (imported here as `taint` — the
rules; this module is the CLI-verb wrapper around them).
"""
from __future__ import annotations

import collections
import contextlib
import importlib.util as _ilu
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import ujson as json

from fae.driver import common
from fae.driver import ops
from fae.driver import render
from fae.driver import state
from fae.driver import validate as taint
from fae.driver.common import ROOT, faults, ledger

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
    boxes = state.containers()
    for cid in ops.select_cells(getattr(args, "selector", None) or "all"):
        ws = common.WS / cid
        st = state.cell_state(ws, {}, boxes)
        if not st or st["state"] != "DONE" or st["why"] == "cancelled":
            continue
        doc = taint._validate_cell(ws)
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
    print("\n--- Scoring --- (one record per cell: defects, iterations-to-green, "
          "authored surface -> <cell>/score.json, which the scoreboard reads)")
    sys.stdout.flush()

    cids = [getattr(args, "cell")] if getattr(args, "cell", None) else ops.select_cells("all")
    boxes = state.containers()
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
        st = state.cell_state(common.WS / cid, {}, boxes)
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
                rc = _sc.score_one(cid)
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
    if getattr(args, "condition", None):
        argv += ["--condition", args.condition]
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


# --- grade: the defect scan ---------------------------------------------------
# One defects.json per terminal cell: the experiment's count_defects.py extracts the
# mechanical evidence, the judge model grades it twice (two separate `claude -p`
# calls, no shared context), its merge_codes.py merges the replies and
# records grader_model. Restartable per pass: a cell whose defects.json validates
# is skipped; one carrying only `codes` pays for pass b alone.
GRADING = common.experiment_dir() / "scoring"     # taxonomy, extractor, merger
TAXONOMY = GRADING / "defect_taxonomy.md"
JUDGE_PREFLIGHT_PROMPT = "reply with exactly: ok"
COST_LOG_HEADER = ("ts\tcell\tmodel\tevidence_bytes\tinput\tcache_create\t"
                   "cache_read\toutput\tcost_usd\tduration_ms\tpass\n")
JUDGE_MODEL_MISSING = (
    "ERROR: JUDGE_MODEL is required (no default — the judge is a recorded\n"
    "       experimental parameter). Example:\n"
    "         python3 cli.py results grade --judge-model claude-opus-5\n"
    "       Use --no-judge (JUDGE=0) for mechanical extraction with no judge at all.")


def _claude_p(model, prompt, json_out):
    """One `claude -p` call: (rc, stdout+stderr merged). Tests stub this."""
    argv = ["claude", "-p", "--model", model]
    if json_out:
        argv += ["--output-format", "json"]
    argv.append(prompt)
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       text=True, errors="surrogateescape")
    return r.returncode, r.stdout


def _read_text(path):
    try:
        return Path(path).read_text(errors="surrogateescape")
    except OSError:
        return ""


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def defects_valid(path, judge=True):
    """The skip rule: parses, schema 2, attempts and authored present, and —
    unless the judge is off — BOTH passes filled. A cell carrying only
    `codes` still owes its second grading."""
    d = _load_json(path)
    if not isinstance(d, dict):
        return False
    ok = d.get("schema") == 2 and "attempts" in d and "authored" in d
    if judge:
        ok = ok and d.get("codes") is not None and d.get("codes_b") is not None
    return bool(ok)


def _has_pass(path, key):
    d = _load_json(path)
    return isinstance(d, dict) and d.get(key) is not None


def _evidence_pack(ws, draft_text):
    """The judge's evidence: draft defects.json, the ledger, every feedback
    prompt's last 60 lines, the first 200 lines of each authored file."""
    ws = Path(ws)
    parts = ["## MECHANICAL EVIDENCE (draft defects.json)\n", draft_text,
             "\n## iterations.log\n", _read_text(ws / "iterations.log"),
             "\n## feedback prompts (contain per-attempt verify/deploy tails)\n"]
    for f in sorted(ws.glob("PROMPT.attempt-*.md")):
        if f.is_file():
            parts.append(f"--- {f.name} (tail)\n")
            parts.append("".join(_read_text(f).splitlines(keepends=True)[-60:]))
    parts.append("\n## authored files (agent surface, current content, "
                 "first 200 lines each)\n")
    d = json.loads(draft_text)
    for rel in d["authored"]["files_added"] + d["authored"]["files_modified"]:
        p = ws / "artifacts" / rel
        if p.is_file():
            parts.append(f"--- {rel}\n")
            parts.append("".join(_read_text(p).splitlines(keepends=True)[:200]))
    return "".join(parts)


def _judge_prompt(evidence):
    """Taxonomy + instructions + evidence. Trailing newlines are dropped, as
    the shell's `$(cat prompt)` did."""
    return (
        "You are grading defects for one experiment cell, using this taxonomy:\n"
        "\n" + _read_text(TAXONOMY) + "\n"
        "Evidence for the cell follows. Grade ONLY defects that are evidenced\n"
        "(per attempt where identifiable). POL1 (policy infidelity) is tracked\n"
        "separately from CAD. If nothing is evidenced, return an empty list.\n"
        "Reply with STRICT JSON only, no prose, exactly this shape:\n"
        '{"codes": [{"attempt": <int|null>, "code": "CAD1..CAD6|POL1", '
        '"artifacts": ["..."], "description": "..."}], "faithful": <bool>}\n'
        "\n" + evidence
    ).rstrip("\n")


def _judge_wall(rc, text):
    """faults.classify over a judge call's output: the json envelope is one
    result line, so it is fed as such; plain text is judged with the rc."""
    try:
        d = json.loads(text)
    except Exception:
        d = None
    if isinstance(d, dict):
        return faults.classify(json.dumps(d), rc)
    return faults.classify(text, rc)


def _cost_row(cost_log, cell, model, evidence_bytes, env, label):
    u = env.get("usage") or {}
    row = (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           cell, model, evidence_bytes,
           u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
           u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0),
           env.get("total_cost_usd", 0), env.get("duration_ms", 0), label)
    with open(cost_log, "a") as f:
        f.write("\t".join(str(x) for x in row) + "\n")


def _judge_pass(model, prompt, cell, evidence_bytes, label, cost_log):
    """One judge invocation. Returns ("ok", reply_text), ("wall", kind, reason)
    or ("fail", detail)."""
    rc, text = _claude_p(model, prompt, json_out=True)
    wall = _judge_wall(rc, text)
    if wall:
        return "wall", wall, faults.reason(text)
    if rc != 0:
        return "fail", "judge call FAILED\n" + "\n".join(text.splitlines()[-3:])
    try:
        env = json.loads(text)
        assert isinstance(env, dict)
    except Exception:
        return "fail", "could not unwrap judge envelope"
    _cost_row(cost_log, cell, model, evidence_bytes, env, label)
    return "ok", env.get("result") or ""


def _grade_cell(ws, out, opts, err):
    """Extract, then grade the passes this cell still owes. Returns
    scanned | skipped | failed | wall."""
    cell = ws.name
    treatment = [l[len("TREATMENT="):] for l in _read_text(ws / "cell.env").splitlines()
                 if l.startswith("TREATMENT=")]
    ref = common.definition().reference_workspace(treatment[0] if treatment else "")
    ref_ws = [str(common.WS / ref)] if ref else []
    print(f"scanning {cell} ...", file=err)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        r = subprocess.run(["python3", str(GRADING / "count_defects.py"), str(ws), *ref_ws],
                           cwd=ROOT, stdout=subprocess.PIPE, text=True,
                           errors="surrogateescape")
        if r.returncode != 0:
            print(f"  extraction FAILED for {cell}", file=err)
            return "failed"
        draft = td / "draft.json"
        draft.write_text(r.stdout, errors="surrogateescape")
        if not opts["judge"]:
            shutil.move(str(draft), str(out))
            print(f"  -> {out} (no judgment)", file=err)
            return "scanned"

        evidence = _evidence_pack(ws, r.stdout)
        prompt = _judge_prompt(evidence)
        evidence_bytes = len(evidence.encode("utf-8", "surrogateescape"))
        need = {"a": True, "b": True}
        if not opts["force"] and out.is_file():
            need["a"] = not _has_pass(out, "codes")
            need["b"] = not _has_pass(out, "codes_b")
        margs = []
        for label in ("a", "b"):
            if not need[label]:
                continue
            res = _judge_pass(opts["judge_model"], prompt, cell, evidence_bytes,
                              label, opts["cost_log"])
            if res[0] == "wall":
                print(f"  pass {label}: judge WALL ({res[1]}) for {cell}: {res[2]}",
                      file=err)
                return "wall"
            if res[0] == "fail":
                print(f"  pass {label}: {res[1]} for {cell}", file=err)
                print(f"  grading FAILED for {cell} (will retry on next run)", file=err)
                return "failed"
            reply = td / f"reply_{label}.txt"
            reply.write_text(res[1], errors="surrogateescape")
            margs += [f"--{label}", str(reply)]
        if not margs:
            return "skipped"
        m = subprocess.run(["python3", str(GRADING / "merge_codes.py"), str(draft), str(out),
                            opts["judge_model"], *margs], cwd=ROOT)
        if m.returncode != 0:
            print(f"  grading FAILED for {cell} (will retry on next run)", file=err)
            return "failed"
        done = "".join(f" {l}" for l in ("a", "b") if need[l])
        print(f"  -> {out} (pass{done})", file=err)
        return "scanned"


def _grade_opts(args):
    """Flags win; the environment (JUDGE_MODEL, FORCE, JUDGE, CELLS, LIMIT,
    COST_LOG, GRADE_INFLIGHT) is the fallback."""
    E = os.environ
    cells = getattr(args, "cells", None) or E.get("CELLS", "").split()
    return {
        "judge_model": getattr(args, "judge_model", None) or E.get("JUDGE_MODEL") or "",
        "judge": not getattr(args, "no_judge", False) and E.get("JUDGE", "1") != "0",
        "force": bool(getattr(args, "force", False)) or E.get("FORCE") == "1",
        "cells": set(cells),
        "limit": int(getattr(args, "limit", None) or E.get("LIMIT", 0) or 0),
        "cost_log": Path(getattr(args, "cost_log", None) or E.get("COST_LOG")
                         or common.WS / ".orch" / "defect-judge-cost.tsv"),
        "inflight": bool(getattr(args, "grade_inflight", False)) or E.get("GRADE_INFLIGHT") == "1",
    }


def grade(args):
    """Defect scan over WS: one defects.json per terminal cell. Returns the
    tally; exits 2 without a judge model, 1 when the judge is unreachable or
    walled (a wall stops the sweep — the lane must cool, not fail every cell)."""
    err = sys.stderr
    opts = _grade_opts(args)
    if opts["judge"]:
        if not opts["judge_model"]:
            print(JUDGE_MODEL_MISSING, file=err)
            sys.exit(2)
        # fail before the first evidence pack is built, not on cell 1 of 30
        rc, text = _claude_p(opts["judge_model"], JUDGE_PREFLIGHT_PROMPT, json_out=False)
        wall = _judge_wall(rc, text)
        if wall or rc != 0 or not re.search(r"\bok\b", text, re.I):
            what = f"WALLED ({wall}): {faults.reason(text)}" if wall else "not reachable:"
            print(f"ERROR: judge model '{opts['judge_model']}' {what}", file=err)
            if not wall:
                print("\n".join(text.splitlines()[-3:]), file=err)
            sys.exit(1)
        print(f"judge model: {opts['judge_model']}", file=err)
        opts["cost_log"].parent.mkdir(parents=True, exist_ok=True)
        if not (opts["cost_log"].is_file() and opts["cost_log"].stat().st_size > 0):
            opts["cost_log"].write_text(COST_LOG_HEADER)

    tally = {"scanned": 0, "skipped": 0, "failed": 0, "wall": 0}
    for ws in sorted(common.WS.iterdir()):
        if not (ws / "cell.env").is_file():
            continue
        if opts["limit"] > 0 and tally["scanned"] >= opts["limit"]:
            print(f"LIMIT={opts['limit']} reached — stopping", file=err)
            break
        if opts["cells"] and ws.name not in opts["cells"]:
            continue
        # terminal cells only: an in-flight cell's artifacts are still
        # changing under the judge. THE ledger decides, never metrics.json.
        if not opts["inflight"]:
            try:
                terminal = ledger.parse(ws)["verdict"] in ("green", "failed", "revoked")
            except Exception:
                terminal = False
            if not terminal:
                continue
        out = ws / "defects.json"
        if not opts["force"] and out.is_file() and defects_valid(out, opts["judge"]):
            tally["skipped"] += 1
            continue
        res = _grade_cell(ws, out, opts, err)
        tally[res] += 1
        if res == "wall":
            break
    print(f"defect scan: {tally['scanned']} scanned, {tally['skipped']} skipped (valid), "
          f"{tally['failed']} failed" + (" — stopped on a judge WALL" if tally["wall"] else ""),
          file=err)
    if tally["wall"]:
        sys.exit(1)
    return tally
