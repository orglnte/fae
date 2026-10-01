"""The post-DONE trust check: turns a terminal cell's raw evidence (logs,
metrics, iterations.log) into validation.json — the taint rule set that
decides VALID vs TAINTED before a cell enters the corpus. The engine's rules
are here; the experiment's (what a rig fault looks like in its evidence) are
the definition's `taint_rules`, run alongside.
"""
from __future__ import annotations

import os
import re
import ujson as json
from pathlib import Path

import csv
from datetime import datetime, timezone

from fae import archive as _archive
from fae import metrics as _metrics
from fae.driver import common
from fae.driver.common import (
    parse_cell_id, ledger, faults, SEAL_MARKER, VALIDATION,
)

# The rules this validator keeps are the engine's own: a provider wall
# charged as an attempt, a reverify aborted on a rig fault, an unreliable
# footprint. What a rig fault looks like in THIS experiment's evidence — the
# mount episodes, the load numbers, the store ceiling, the junction defects —
# is the definition's `taint_rules` (experiment/taint.py).


def _validate_cell(ws):
    """All checks for one DONE cell. Returns the validation dict (also
    written to $ws/validation.json). Re-runnable: rules can improve and be
    re-applied retroactively — the file records the rule set's verdict."""
    taints, warns = [], []
    rc_text = common.definition().report_text(ws)
    v_log = ws / "verify.log"
    v_text = v_log.read_text(errors="replace") if v_log.exists() else ""
    it_text = ((ws / "iterations.log").read_text(errors="replace")
               if (ws / "iterations.log").exists() else "")
    metrics = _metrics.read(ws)

    # rig aborts
    if "ERROR[rig]" in it_text:
        warns.append("a reverify aborted on a rig fault (green intact, "
                     "gate incomplete)")
    # An attempt charged on a provider wall. The driver waits (limit) or
    # halts (auth) on these; a charged one is an attempt that was never a
    # build, so attempts-to-green is wrong for the cell. The exit code is
    # unknown after the fact: the transcript alone decides, as it does for an
    # untouched tree in the driver.
    for m in re.finditer(r"\tITER\t(?:fail|budget)\tattempt=(\d+)([^\n]*)", it_text):
        n = int(m.group(1))
        try:
            text = (ws / f"agent.attempt-{n}.log").read_text(errors="replace")
        except OSError:
            continue
        # A structured result line always decides. A plain transcript decides
        # only for an untouched tree (stage=no-edit) — the same gate the
        # driver applies, for the same reason.
        kind = faults.classify(text, 1 if "stage=no-edit" in m.group(2) else 0)
        if kind:
            taints.append(f"attempt {n} charged on a provider {kind} wall: "
                          f"{faults.reason(text)[:80]}")
    try:
        res = json.loads((ws / "resources.json").read_text())
    except (OSError, json.JSONDecodeError):
        res = None
    if res is not None and "reliable" in res and not res["reliable"]:
        # the footprint's two sources disagreed, or it was sampled
        # machine-wide: the number exists but is not to be quoted
        warns.append(f"footprint unreliable (scope={res.get('scope')}, "
                     f"{res.get('unreliable_samples')} sample(s) disagreed: "
                     f"{'; '.join(res.get('cross_check_findings') or [])[:120]})")
    warns += archive_warns(ws, it_text)
    # the experiment's rules, and the fields it records beside the verdict
    rules = common.definition().taint_rules
    verdict = ledger.parse(ws)["verdict"]
    xt, xw, fields = rules(ws, common.WS, metrics, it_text, v_text, rc_text, verdict) \
        if rules else ([], [], {})
    taints += xt
    warns += xw
    doc = {"verdict": "TAINTED" if taints else "VALID",
           "taints": taints, "warns": warns,
           **fields,
           # `ceilings_all_min_max` was always [x, x] — it aliased the single
           # deciding ceiling while its NAME promised a range across attempts.
           # A field that reports a range nothing measures is worse than no
           # field: rule_set 3 drops it rather than keep publishing it.
           # rule_set 5 adds the provider-wall rule above; rule_set 6 the
           # rig<->framework junction rules 12-16 (the experiment's).
           # rule_set 7 adds the archive checks (runs not charged, and the
           # archive disagreeing with the ledger).
           "rule_set": 7,
           "at": f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"}
    (ws / VALIDATION).write_text(json.dumps(doc, indent=1))
    record_taints_on_seal(ws, taints)
    return doc
NOT_CHARGED_WARN_AT = 3


def archive_warns(ws, it_text):
    """What the archive's runs that were not charged, and the archive against
    the ledger, say about the rig. Warnings: the verdict still stands, but a
    rig that keeps refunding, or an archive that disagrees with the ledger,
    is something to look at."""
    warns = []
    runs = _archive.runs(ws)
    free = [r for r in runs if r.end_state in _archive.NOT_CHARGED]
    by_stage = {}
    for r in free:
        stage = r.verdict().get("stage") or r.end_state
        by_stage[stage] = by_stage.get(stage, 0) + 1
    if len(free) >= NOT_CHARGED_WARN_AT or any(n > 1 for n in by_stage.values()):
        detail = ", ".join(f"{k}x{n}" for k, n in sorted(by_stage.items()))
        warns.append(f"{len(free)} verify run(s) not charged ({detail})")
    labelled = [r for r in runs if r.end_state is not None and r.attempt]
    if not labelled:
        return warns
    first = min(r.attempt for r in labelled)
    ledger_state = {}
    for m in re.finditer(r"\tITER\t(fail|budget|green)\tattempt=(\d+)([^\n]*)", it_text):
        n = int(m.group(2))
        if n < first or "stage=no-edit" in m.group(3):
            continue
        ledger_state[n] = "green" if m.group(1) == "green" else "charged"
    archive_state = {}
    for r in labelled:
        if r.end_state == "charged":
            archive_state[r.attempt] = "charged"
        elif r.end_state == "green":
            archive_state.setdefault(r.attempt, "green")
    for n in sorted(set(ledger_state) | set(archive_state)):
        led, arc = ledger_state.get(n), archive_state.get(n)
        if led != arc:
            warns.append(f"archive/ledger mismatch at attempt {n}: "
                         f"ledger {led or 'no verdict'}, archive {arc or 'no judged run'}")
    return warns


def record_taints_on_seal(ws, taints):
    """The seal carries the validator's taints as `taint=` lines, so a
    reader of the cell sees why it is excluded without opening
    validation.json. Rewritten on every validate; unchanged content is
    left alone (the marker is read-only)."""
    p = Path(ws) / SEAL_MARKER
    try:
        old = p.read_text()
    except OSError:
        return
    keep = [l for l in old.splitlines() if not l.startswith("taint=")]
    new = "\n".join(keep + [f"taint={t}" for t in taints]) + "\n"
    if new == old:
        return
    try:
        p.chmod(0o644)
        p.write_text(new)
    finally:
        p.chmod(0o444)
