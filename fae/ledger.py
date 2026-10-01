#!/usr/bin/env python3
"""THE iterations.log library — one parser, one outcome derivation.

The ledger is the experiment's file of record: TAB-separated events, one per
line, `<ts> <EVENT> <...fields>`. Before this module there were five
independent parsers (runs.py cell_state, run_cell's awk, reverify's
metrics.json dissenter, score_cell, count_defects) and every historical
data-integrity incident was two of them disagreeing — revoked greens scored
as green, mid-gate resumes nearly minting ungated greens, stranded reverifies
leaving the reverification population silently. One derivation, imported
everywhere, makes a verdict bug a one-file fix.

Event vocabulary (v1, implicit — no header line):
  START    attempt began
  ITER     attempt judged: fail | green | budget (the scientific datum)
  END      cell finished: green=true | green=false
  HALT     rig fault, no attempt burned, resumable
  WAIT     transient API fault, same attempt retries
  PAUSED   operator pause honored at a safe point
  NOEDIT   attempt modified no files (verify skipped, attempt spent)
  HEAL     out-of-surface edit reverted before verify
  SHAPE    one shape-gate arrangement verdict (in-run or reverify)
  REVERIFY 6-shape gate lifecycle on a frozen green (start/PASS/REVOKED/ERROR)
v2 additions (written going forward, absence tolerated):
  PREPARED workspace created (kills the never-started sentinel)
  VERIFY_READY   bring-up succeeded AND /health held for the confirmation
                 window — the verify genuinely started
  VERIFY_NOSTART it did not: no attempt burned, same attempt retried on the
                 next start. Distinct from HALT so the consecutive count is
                 greppable and a permanently broken environment can escalate.
  REVERIFY_END  explicit gate-completion marker (upheld/revoked/rig-error) —
                completion used to be INFERRED from a REVERIFY line's payload
                text; old ledgers lack it, so the inference stays as fallback

Outcome verdicts (intent like .cancelled is NOT the ledger's business):
  green | failed | revoked | None (in flight)
"""
from __future__ import annotations

import re
from pathlib import Path

SCHEMA = 2


def parse(ws: Path, gate_n: int = 6) -> dict:
    """Parse a workspace's ledger into raw events + everything every consumer
    needs. Single pass, no I/O beyond the one read. Returns a dict:

    events        int   — count of well-formed event lines
    iters         [str] — ITER results in order (fail|green|budget)
    iter_notes    [str] — ITER note fields (attempt=N stage=... e2e=...)
    att           int   — attempts recorded (len(iters))
    last_ev/_line str   — last event, its full line (reverify-adjusted: an
                          in-flight gate keeps the prior END authoritative)
    last_end_green bool|None — verdict of the last END line
    green_seen    bool  — any END green=true ever
    reverified    bool  — any REVERIFY event
    reverify_active bool — gate opened, no completion yet
    rev_pass      int   — arrangements passed in the open gate
    prepared      bool  — v2 PREPARED seen
    verdict       str|None — green | failed | revoked | None (in flight)
    green_at      int|None — attempt number of the green ITER
    halt_cause    str   — tail of the last HALT line ("" if none)
    noedit        int   — attempts the agent ended without editing anything
    noedit_last   str   — tail of the last NOEDIT line ("" if none)
    alerts        int   — ALERT events (setup failures)
    alerts_open   int   — ALERT events since the last START (unanswered)
    alert_last    str   — tail of the last ALERT line ("" if none)
    """
    log = ws / "iterations.log"
    iters: list[str] = []
    notes: list[str] = []
    n_events = 0
    last_ev, last_line, last_end = "", "", ""
    last_end_green = None
    green_seen = reverified = prepared = False
    rev_start, rev_pass, rev_done = -1, 0, False
    halt_cause = ""
    noedit = alerts = alerts_open = 0
    noedit_last = alert_last = ""
    shape_pass_by_att: dict[str, int] = {}
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue                      # v2 header/comment lines
        f = line.split("\t")
        if len(f) < 3:
            continue
        ev = f[1]
        n_events += 1
        last_ev, last_line = ev, line
        if ev == "ITER":
            iters.append(f[2])
            notes.append(f[3] if len(f) > 3 else "")
        elif ev in ("END", "HALT", "VERIFY_NOSTART"):
            last_end = line
            if ev == "END":
                last_end_green = "green=true" in line
                if last_end_green:
                    green_seen = True
            else:
                halt_cause = line.split("\t")[-1][:70]
        elif ev == "NOEDIT":
            noedit += 1
            noedit_last = f[-1][:110]
        elif ev == "ALERT":
            alerts += 1
            alerts_open += 1
            alert_last = f[-1][:110]
        if ev == "START":
            alerts_open = 0           # a (re)start is the operator's answer
        if ev == "PREPARED":
            prepared = True
        if ev == "REVERIFY":
            reverified = True
            if "start" in line:
                rev_start, rev_pass, rev_done = i, 0, False
        if ev == "SHAPE" and "reverify" not in line and line.rstrip().endswith("pass"):
            m = re.search(r"attempt=(\d+)", line)
            if m:
                shape_pass_by_att[m.group(1)] = shape_pass_by_att.get(m.group(1), 0) + 1
        if rev_start >= 0 and not rev_done and ev == "REVERIFY_END":
            # explicit completion marker (preferred going forward) — old
            # ledgers never have this line, so the text-inference branches
            # below stay untouched as the fallback for them.
            rev_done = True
        elif rev_start >= 0 and not rev_done and ev != "REVERIFY":
            if ev == "SHAPE" and "reverify" in line and line.rstrip().endswith("pass"):
                rev_pass += 1
            elif ev in ("END", "HALT", "VERIFY_NOSTART"):
                rev_done = True
        elif rev_start >= 0 and not rev_done and ev == "REVERIFY" and \
                any(k in line for k in ("PASS", "REVOKED", "ERROR")):
            rev_done = True
    reverify_active = rev_start >= 0 and not rev_done
    if last_end and last_ev not in ("END", "HALT", "ITER", "START", "VERIFY_NOSTART"):
        # trailing non-attempt events (SHAPE/REVERIFY/PAUSED bookkeeping)
        # never un-finish a cell: the last END/HALT stays authoritative.
        # (2026-07-25: a spurious appended REVERIFY line hid 7 verdicts.)
        last_ev, last_line = last_end.split("\t")[1], last_end

    verdict = None
    green_at = None
    if last_ev == "END":
        if "green=true" in last_line:
            verdict = "green"
            green_at = len(iters)
        elif green_seen and reverified:
            # the gate revoked an earlier green: solved once, not
            # shape-robust — never to be merged with never-solved-it
            verdict = "revoked"
        else:
            verdict = "failed"
    if "green" in iters and green_at is None:
        green_at = iters.index("green") + 1

    # GATE progress of the LAST judged attempt, over gate_n arrangements:
    # N/N = full gate (green), 1+k/N = primary verify passed + k arrangements
    # before the gate failed, 0/N = failed the primary verify (gate never
    # opened), None = no shape info (legacy / no verify yet).
    gate = None
    if notes and "shapes=all" in notes[-1]:
        gate = gate_n
    elif notes:
        m = re.search(r"attempt=(\d+)", notes[-1])
        att_no = m.group(1) if m else str(len(iters))
        if "shape-gate=" in notes[-1]:
            gate = 1 + shape_pass_by_att.get(att_no, 0)
        elif "stage=" in notes[-1]:
            gate = 0

    # LIVE shape-gate progress: arrangements passed so far in the attempt
    # that has NOT been judged yet (no ITER line for it), as opposed to
    # `gate` above which is frozen at the LAST JUDGED attempt. A cell deep
    # in a 6-arrangement shape gate shows gate=0/6 (last judged attempt
    # never reached the gate) for its whole in-flight duration otherwise —
    # this field is what a live status display should read instead.
    live_shape_pass = shape_pass_by_att.get(str(len(iters) + 1), 0)

    return dict(events=n_events, iters=iters, iter_notes=notes, gate=gate, gate_n=gate_n,
                att=len(iters), last_ev=last_ev, last_line=last_line,
                last_end_green=last_end_green, green_seen=green_seen,
                reverified=reverified, reverify_active=reverify_active,
                rev_pass=rev_pass, prepared=prepared, verdict=verdict,
                green_at=green_at, halt_cause=halt_cause,
                live_shape_pass=live_shape_pass,
                noedit=noedit, noedit_last=noedit_last,
                alerts=alerts, alerts_open=alerts_open, alert_last=alert_last)


def hist(parsed: dict) -> str:
    """The LAST token of the stage history, which is what status renders in
    its `LAST REP` column — not the whole history, despite the run-length
    compression below building one. One token per attempt is computed
    ('<failed-stage> e2e:OK|a/b [gate=XYZ]' for fails, 'green' for the green),
    consecutive identical ones are collapsed, and only the final token is
    returned. e2e:OK = full e2e pass (the failure was later, e.g. the
    scaling law); a partial fraction means e2e itself failed."""
    out = []
    for i, n in enumerate(parsed["iter_notes"]):
        verdict = parsed["iters"][i] if i < len(parsed["iters"]) else ""
        if verdict == "green":
            out.append("green")
            continue
        kv = dict(p.split("=", 1) for p in n.split() if "=" in p)
        stage = kv.get("stage", verdict or "?")
        e2e = kv.get("e2e", "")
        a, _, b = e2e.partition("/")
        # e2e shown only when it FAILED — a full pass is the normal case and
        # repeating it per attempt read as duplication
        e2e = "" if (a == b and a not in ("", "0")) or not e2e else f"e2e NOK:{e2e}"
        gate = f"gate={kv['shape-gate']}" if "shape-gate" in kv else ""
        out.append(" ".join(t for t in (stage, e2e, gate) if t))
    # run-length compress consecutive identical attempts: 'scaling ×7' beats
    # seven copies of 'scaling'
    rle: list[str] = []
    for tok in out:
        if rle and rle[-1].split(" ×")[0] == tok:
            base = rle[-1].split(" ×")
            rle[-1] = f"{tok} ×{int(base[1]) + 1 if len(base) > 1 else 2}"
        else:
            rle.append(tok)
    return rle[-1] if rle else ""


# --- writing ------------------------------------------------------------------
# One writer for every producer: the cell, its shims, and supervision.
#
# No lock. O_APPEND makes seek-to-end + write atomic per open file description,
# so concurrent producers never interleave — PROVIDED each record is one
# complete line written in a single call, which is what append() guarantees.
# Measured on this platform at 12 concurrent writers and at 64KB lines.
# (Not true over NFS, where O_APPEND is not atomic.)

ITER_RESULTS = ("fail", "green", "budget")
FIELD_MAX = 400


def _clean(value) -> str:
    """A tab or a newline inside a field breaks the TSV whatever the kernel
    did with the write, so free text is sanitised rather than trusted."""
    s = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return s[:FIELD_MAX]


def _stamp() -> str:
    from datetime import datetime, timezone
    return f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def append(ws, event: str, *fields) -> str:
    """Append ONE event. Returns the line written.

    The whole record is built first and written once — splitting it across
    writes is what would let two producers interleave.
    """
    line = "\t".join([_stamp(), _clean(event), *(_clean(f) for f in fields)]) + "\n"
    log = Path(ws) / "iterations.log"
    with log.open("a") as f:
        f.write(line)
    return line


def record_iter(ws, result: str, note: str = "") -> str:
    """The one ITER writer. iterations-to-green is counted from these lines, so
    the result word is validated and an unprepared workspace is refused."""
    if result not in ITER_RESULTS:
        raise ValueError(f"RESULT must be one of {'|'.join(ITER_RESULTS)} "
                         f"(got {result!r})")
    if not (Path(ws) / "iterations.log").is_file():
        raise FileNotFoundError(f"no iterations.log in {ws} — prepare the cell first")
    return append(ws, "ITER", result, note)


def alert(ws, cid: str, detail: str) -> str:
    """Supervision's channel: written ABOUT a cell, by the supervisor, when the
    cell is wedged or its process is gone and cannot report for itself."""
    return append(ws, "ALERT", cid, detail)


def main(argv=None):
    """CLI: python3 fae/ledger.py CELL_ID RESULT [note]."""
    import os
    import sys
    a = argv if argv is not None else sys.argv[1:]
    if len(a) < 2:
        print("usage: CELL_ID RESULT [note]", file=sys.stderr)
        return 1
    cid, result, note = a[0], a[1], (a[2] if len(a) > 2 else "")
    ws_dir = os.environ.get("WORKSPACES_DIR")
    if not ws_dir:
        from fae import paths
        root = paths.root()
        from fae.cell import config as _config
        ws_dir = _config.load(root).get("WORKSPACES_DIR")
    try:
        record_iter(Path(ws_dir) / cid, result, note)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
