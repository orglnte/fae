#!/usr/bin/env python3
"""tla_verify — fae's PlusPy-driven exhaustive checker for TLA+ models.

A repo carries its model in .tla/<Name>.tla; this one script validates any
of them, and replays a recorded transitions log against one (--live-trace).
The spec is SELF-DESCRIBING via structured header comments:

    \\* CHECK-CONSTANTS: Cells={"c1","c2"}; Budget=2; Slots=1
    \\* CHECK-ACTIONS: Spawn AcquireSlot StandDown ... (cell-argument ops)
    \\* CHECK-INVARIANTS: TypeOK VerifyMutualExclusion ...
    \\* CHECK-ARGS: Cells          (the constant whose members are action args)

Exhaustiveness contract (the "checker-friendly" style this checker
requires): every CHECK-ACTION is a named operator taking one argument from
CHECK-ARGS, deterministic given that argument — no internal \\E. The driver
asserts determinism on every satisfiable (action, arg) pair; violating the
style fails loudly instead of silently under-checking.

Usage:  tla_verify [path/to/Spec.tla]      (default: .tla/*.tla in cwd)
Exit 0 = every reachable state satisfies every CHECK-INVARIANT.
"""
from __future__ import annotations

import ast
import glob
import os
import re
import sys
from collections import deque

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(TOOLS, "pluspy"))
import pluspy as pp_mod  # noqa: E402


def load_header(path):
    text = open(path).read()
    def field(name, required=True):
        m = re.search(rf"\\\* CHECK-{name}:\s*(.+)", text)
        if not m:
            if required:
                sys.exit(f"tla_verify: {path} missing '\\* CHECK-{name}:' header")
            return None
        return m.group(1).strip()
    consts = {}
    for part in field("CONSTANTS").split(";"):
        k, _, v = part.partition("=")
        val = ast.literal_eval(v.strip())
        if isinstance(val, (set, frozenset, list, tuple)):
            val = frozenset(val)
        consts[k.strip()] = val
    actions = field("ACTIONS").split()
    invariants = field("INVARIANTS").split()
    argsrc = field("ARGS")
    if argsrc not in consts:
        sys.exit(f"tla_verify: CHECK-ARGS '{argsrc}' is not a CHECK-CONSTANT")
    return consts, actions, invariants, sorted(consts[argsrc])


def _fcn_items(val):
    """The domain->value mapping of a TLA+ function value, or None if `val`
    is not a function. PlusPy's simplify() renders a function whose domain is
    1..n as a tuple (and a tuple of characters as a str), and the empty
    function as ""; all three are functions in TLA+."""
    if isinstance(val, pp_mod.FrozenDict):
        return val.d
    if isinstance(val, (tuple, str)):
        return {i: x for i, x in enumerate(val, 1)}
    return None


def _eval_pred_lazy(expr, containers):
    """Evaluate an unprimed boolean expression, rewriting the two membership
    forms whose PlusPy evaluation materialises an exponentially large set
    into the pointwise definitions TLA+ gives them:

        f \\in [D -> R]   ==  DOMAIN f = D /\\ \\A x \\in D : f[x] \\in R
        S \\in SUBSET D   ==  S \\subseteq D

    Same boolean, different cost. PlusPy's FuncsetExpression.eval builds all
    |R|^|D| functions, and its SUBSET builds all 2^|D| subsets, BEFORE testing
    membership. A TypeOK written in the ordinary style is therefore
    unevaluable off a live trace, where |D| is the number of cells actually
    observed rather than the two of the exhaustive run: with Cells=50 and
    Budget=100, `attempts \\in [Cells -> 0..Budget]` alone asks for 101^50
    functions. That is the OOM this rewrite exists to avoid; both rewrites are
    O(|D|).

    Only the boolean connectives are descended, and only for unprimed
    expressions -- state predicates -- where Python's short-circuit and/or
    agree with TLA+ and PlusPy's next-state bookkeeping is not in play.
    Everything else falls through to PlusPy's own eval, so the checker never
    approximates: an expression it does not recognise is still evaluated
    exactly, just slowly.
    """
    if expr.primed:
        return expr.eval(containers, {})
    if isinstance(expr, pp_mod.InfixExpression):
        lex = pp_mod.lexeme(expr.op)
        if lex in ("/\\", "\\land"):
            return (_eval_pred_lazy(expr.lhs, containers)
                    and _eval_pred_lazy(expr.rhs, containers))
        if lex in ("\\/", "\\lor"):
            return (_eval_pred_lazy(expr.lhs, containers)
                    or _eval_pred_lazy(expr.rhs, containers))
        if lex == "=>":
            return (not _eval_pred_lazy(expr.lhs, containers)
                    or _eval_pred_lazy(expr.rhs, containers))
        if lex == "\\in":
            rhs = expr.rhs
            if isinstance(rhs, pp_mod.FuncsetExpression):
                dom = frozenset(rhs.lhs.eval(containers, {}))
                rng = rhs.rhs.eval(containers, {})
                items = _fcn_items(expr.lhs.eval(containers, {}))
                return (items is not None
                        and frozenset(items.keys()) == dom
                        and all(v in rng for v in items.values()))
            if isinstance(rhs, pp_mod.BuiltinExpression) and rhs.id == "SUBSET":
                base = frozenset(rhs.args[0].eval(containers, {}))
                return frozenset(expr.lhs.eval(containers, {})) <= base
    if isinstance(expr, pp_mod.BuiltinExpression) \
            and expr.id in ("~", "\\lnot", "\\neg") and len(expr.args) == 1:
        return not _eval_pred_lazy(expr.args[0], containers)
    return expr.eval(containers, {})


def _pp_helpers(pp):
    """The three PlusPy accessors both drivers need: read the whole state,
    write it back, and evaluate a named state predicate in it."""
    def getall():
        return dict(pp.getall().d)

    def set_state(state):
        for var, val in state.items():
            pp.set(var, val)

    def eval_pred(opname):
        op = pp.mod.operators[opname]
        for c in pp.containers.values():
            c.prev = c.next
        expr = op.expr.substitute(pp.constants).substitute(pp.containers)
        return _eval_pred_lazy(expr, pp.containers)

    return getall, set_state, eval_pred


def _fmt_cell(state, cid):
    """A PlusPy state projected onto one cell -- what a violation report needs.
    Function-valued variables show that cell's entry; set-valued ones (small by
    construction) show whole. Without this the state renders as a row of
    <pluspy.FrozenDict object at 0x...>, which is what a violation report can
    least afford."""
    parts = []
    for var in sorted(state):
        val = state[var]
        if hasattr(val, "d"):
            if cid in val.d:
                parts.append(f"{var}[{cid}]={val.d[cid]!r}")
        else:
            parts.append(f"{var}={sorted(val)!r}")
    return "  ".join(parts)


def _parse_transitions(path):
    """Parse a global transitions.log: <ts>\\t<ACTION>\\t<cid>\\t<k=v ...>.
    Returns a time-sorted list of dicts. ISO8601 sorts lexicographically, so
    a plain sort is a correct tie-break-stable order even across writers."""
    import re as _re
    events = []
    for n, line in enumerate(open(path)):
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) < 3:
            continue
        ts, action, cid = f[0], f[1], f[2]
        extra = f[3] if len(f) > 3 else ""
        kv = dict(_re.findall(r"(\S+)=(\S+)", extra))
        events.append({"ts": ts, "action": action, "cid": cid, "kv": kv, "n": n})
    events.sort(key=lambda e: e["ts"])
    return events


def _from_last_epoch(events):
    """The events from the log's last run of consecutive EPOCH lines on, in
    file order: a re-anchor is appended, so everything written before it is
    superseded and neither replayed nor judged. No EPOCH: all events."""
    order = sorted(events, key=lambda e: e["n"])
    start = None
    for i in range(len(order) - 1, -1, -1):
        if order[i]["action"] == "EPOCH":
            start = i
        elif start is not None:
            break
    if start is None:
        return events
    cut = order[start]["n"]
    return [e for e in events if e["n"] >= cut]


def _incarnations(events):
    """`Retire <cid>` ends the cell under that id (its workspace was wiped);
    the id's later events are a NEW cell, replayed from Init as `<cid>#<n>`.
    Retire lines are consumed here, never replayed as a spec action — the
    spec's cells never un-finish, and a reused id is not the same cell."""
    gen, out = {}, []
    for e in events:
        cid = e["cid"]
        if e["action"] == "Retire":
            gen[cid] = gen.get(cid, 0) + 1
            continue
        n = gen.get(cid, 0)
        out.append(dict(e, cid=f"{cid}#{n}") if n else e)
    return out


def check_live_trace(path, spec=None, since=None):
    """--live-trace <transitions.log> [spec.tla]: replay the GLOBAL,
    cross-cell event trace through PlusPy against the real spec (not a
    lookalike rule set). Checks:
      - every event is an ENABLED transition given prior history (an
        illegal transition — e.g. two cells both holding AcquireVerify —
        is flagged at the event, with a shortest-trace-style citation)
      - TypeOK, VerifyMutualExclusion, WorkCap, KilledStaysDead hold after
        every step (NoBudgetOverrun is deliberately excluded: it's a
        per-cell property the ledger's own rules check with each cell's
        ATTEMPT_BUDGET (fae/cell/ledger.py: check); the model's one global Budget constant
        can't represent per-treatment budgets)
      - one timing predicate, checked in Python over event timestamps, not
        modeled in TLA+ (wall-clock reasoning doesn't belong in the spec):
        min verify duration

    `since` (ISO-8601 Z) judges only events at or after that instant. Earlier
    events are still REPLAYED — the state they build is what makes later
    events judgeable — but their violations are counted separately and do not
    fail the run. Use it when a fix changes what conformance means: history
    recorded under the old rules is not evidence about the new ones. It does
    not repair a cell whose state desynced before the cutoff; an EPOCH
    re-anchor does that.
    """
    from datetime import datetime as _dt
    if spec is None:
        cands = sorted(glob.glob(".tla/*.tla"))
        if not cands:
            sys.exit("tla_verify --live-trace: no .tla/*.tla in cwd")
        spec = cands[0]
    spec = os.path.abspath(spec)
    _, LIVE_ACTIONS, _, _ = load_header(spec)
    LIVE_INVARIANTS = ("TypeOK", "VerifyMutualExclusion", "WorkCap", "KilledStaysDead")

    events = _incarnations(_from_last_epoch(_parse_transitions(path)))
    if not events:
        print(f"tla_verify --live-trace: no events in {path}")
        return 0
    cells = frozenset(e["cid"] for e in events if e["cid"])
    min_verify_s = int(os.environ.get("TRACE_MIN_VERIFY_S", 45))
    consts = {"Cells": cells, "Budget": 100,
              "Slots": int(os.environ.get("WORK_SLOTS", 7))}

    pp_mod.pluspypath = ":".join(
        [".", os.path.dirname(spec)]
        + [os.path.join(TOOLS, "pluspy", "modules", d)
           for d in ("lib", "book", "other")])
    pp = pp_mod.PlusPy(spec, constants=consts)
    pp.init("Init")
    getall, set_state, eval_pred = _pp_helpers(pp)

    state = getall()

    # --- EPOCH: replay from a RECORDED starting state, not always from Init --
    # A live trace is only replayable from Init if the log begins on a cold
    # fleet — every cell idle, intent "run", no outcome. Reset the log at any
    # other moment and the replay judges real events against a state that never
    # existed: on 2026-07-30 a log reset while 65 cells sat paused turned the
    # following `resume all` into 65 "Resume not ENABLED" violations, plus the
    # cascade behind them, because Resume requires intent="paused".
    #
    # The writer (trace-reset) therefore RECORDS the observed state as a run of
    # EPOCH lines appended to the log. The replay starts at the last such run
    # (_from_last_epoch): its lines are applied over Init, and every event
    # written before it is dropped. Deliberately not inferred from the workspaces at read
    # time: inference would silently absorb the mismatches this check exists to
    # find — a log missing a Pause that must have happened would look identical
    # to a log that never needed one. A recorded epoch stays auditable.
    #
    # Everything after the epoch is checked at full strength; nothing before
    # it is replayed or reported.
    epoch = [e for e in events if e["action"] == "EPOCH"]
    events = [e for e in events if e["action"] != "EPOCH"]
    if epoch:
        seeded = 0
        for e in epoch:
            cid, kv = e["cid"], e["kv"]
            if cid not in cells:
                continue
            for var, key, cast in (("outcome", "outcome", str),
                                   ("intent", "intent", str),
                                   ("loop", "loop", str),
                                   ("attempts", "attempts", int),
                                   ("slotHeld", "slot", lambda s: s == "true")):
                if key not in kv:
                    continue
                fn = state[var]
                d = dict(fn.d)
                d[cid] = cast(kv[key])
                state[var] = type(fn)(d) if hasattr(fn, "d") else fn
            seeded += 1
        # The verify lock is one global set, not a per-cell function: a cell
        # seeded mid-verify whose hold is not recorded cannot legally finish
        # that verify, and every later event for it cascades.
        vheld = frozenset(e["cid"] for e in epoch
                          if e["kv"].get("verify") == "true" and e["cid"] in cells)
        if vheld:
            state["verifyHeld"] = vheld
        print(f"tla_verify --live-trace: seeded {seeded} cell(s) from EPOCH "
              f"({len(cells) - seeded} at Init); replay starts there, not at Init")

    if since:
        judged_from = sum(1 for e in events if e["ts"] >= since)
        print(f"tla_verify --live-trace: judging {judged_from} of "
              f"{len(events)} events (since {since}); earlier events are "
              f"replayed for state only")

    acquire_ts = {}   # cid -> AcquireVerify timestamp
    violations = 0
    stale_violations = 0
    cascaded = 0
    # A not-ENABLED event is NOT applied, so the model's picture of that cell
    # stays behind reality from then on and every later event for it tends to
    # fail too. Those follow-ons are counted (they are still nonconformance)
    # but flagged, so a reader can find the independent faults.
    desynced = set()
    for i, ev in enumerate(events, 1):
        action, cid = ev["action"], ev["cid"]
        cite = f"event #{i}: {action}({cid}) @ {ev['ts']}"
        judged = not since or ev["ts"] >= since
        if action not in LIVE_ACTIONS:
            continue   # e.g. an action this .tla doesn't model — skip, don't fail
        set_state(state)
        if not pp.next(action, cid):
            if not judged:
                # Outside the window: not evidence, but the cell's state now
                # lags reality, so later events for it may cascade.
                stale_violations += 1
                desynced.add(cid)
                continue
            tag = ""
            if cid in desynced:
                tag = " [cascade: this cell already had an unapplied event]"
                cascaded += 1
            print(f"tla_verify --live-trace VIOLATION {cite}: not ENABLED "
                  f"given prior state — illegal transition{tag}")
            print("  state before:", _fmt_cell(state, cid))
            desynced.add(cid)
            violations += 1
            continue
        state = getall()
        for inv in LIVE_INVARIANTS:
            set_state(state)
            if not eval_pred(inv):
                if not judged:
                    stale_violations += 1
                    continue
                print(f"tla_verify --live-trace VIOLATION {cite}: {inv} "
                      f"fails in the resulting state")
                print("  state after:", _fmt_cell(state, cid))
                violations += 1

        # --- timing predicate (Python-side, not modeled in TLA+) ------------
        if action == "AcquireVerify":
            try:
                acquire_ts[cid] = _dt.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                pass
        elif action in ("VerifyGreen", "VerifyFail") and cid in acquire_ts:
            t0 = acquire_ts.pop(cid)
            try:
                t1 = _dt.strptime(ev["ts"], "%Y-%m-%dT%H:%M:%SZ")
                dur = (t1 - t0).total_seconds()
                # A VerifyFail carrying void= was refunded as a substrate HALT:
                # no attempt charged, no verdict judged on the tree. The
                # min-duration floor governs charged verdicts; a voided collapse
                # the rig already declined to score is not one.
                if dur < min_verify_s and not (action == "VerifyFail" and ev["kv"].get("void")):
                    if judged:
                        print(f"tla_verify --live-trace VIOLATION {cite}: verify "
                              f"lasted {dur:.0f}s < {min_verify_s}s (min-verify-duration)")
                        violations += 1
                    else:
                        stale_violations += 1
            except ValueError:
                pass

    stale = (f"; {stale_violations} more before the --since cutoff, not "
             f"judged") if stale_violations else ""
    if violations:
        extra = (f"; {cascaded} of them cascade from an earlier unapplied "
                 f"event on the same cell") if cascaded else ""
        print(f"tla_verify --live-trace: {violations} violation(s) over "
              f"{len(events)} events, {len(cells)} cell(s){extra}{stale}")
        return 1
    print(f"tla_verify --live-trace OK: {len(events)} events, "
          f"{len(cells)} cell(s), 0 violations{stale}")
    return 0


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--live-trace":
        argv, since = list(sys.argv[2:]), None
        if "--since" in argv:
            i = argv.index("--since")
            since = argv[i + 1] if i + 1 < len(argv) else None
            if not since:
                sys.exit("tla_verify --live-trace: --since needs an ISO-8601 Z instant")
            del argv[i:i + 2]
        return check_live_trace(argv[0], argv[1] if len(argv) > 1 else None, since)
    if len(sys.argv) > 1:
        spec = sys.argv[1]
    else:
        cands = sorted(glob.glob(".tla/*.tla"))
        if not cands:
            sys.exit("tla_verify: no .tla/*.tla in cwd (protocol: the model "
                     "lives in .tla/ at the repo root)")
        spec = cands[0]
    spec = os.path.abspath(spec)
    consts, actions, invariants, args = load_header(spec)

    pp_mod.pluspypath = ":".join(
        [".", os.path.dirname(spec)]
        + [os.path.join(TOOLS, "pluspy", "modules", d)
           for d in ("lib", "book", "other")])
    pp = pp_mod.PlusPy(spec, constants=consts)
    pp.init("Init")
    getall, set_state, eval_pred = _pp_helpers(pp)

    def freeze(state):
        return tuple(sorted(state.items()))

    init = getall()
    seen = {freeze(init): None}
    q = deque([init])
    explored = 0
    while q:
        state = q.popleft()
        explored += 1
        set_state(state)
        for inv in invariants:
            if not eval_pred(inv):
                return fail(inv, state, seen)
        for action in actions:
            for a in args:
                set_state(state)
                if pp.next(action, a):
                    if pp.unchanged():
                        continue
                    nxt = getall()
                    set_state(state)
                    assert pp.next(action, a) and getall() == nxt, (
                        f"NONDETERMINISTIC action {action}({a}) — the spec "
                        f"violates the checker-friendly style (no internal "
                        f"\\E); exhaustiveness is void. Fix the spec.")
                    k = freeze(nxt)
                    if k not in seen:
                        seen[k] = (freeze(state), action, a)
                        q.append(nxt)
    print(f"tla_verify OK — {os.path.basename(spec)}: {explored} states, "
          f"invariants: {' '.join(invariants)}")
    return 0


def fail(violation, state, seen):
    print(f"tla_verify VIOLATION: {violation}")
    trace, cur = [], tuple(sorted(state.items()))
    while cur is not None and seen.get(cur) is not None:
        prev, action, a = seen[cur]
        trace.append(f"{action}({a})")
        cur = prev
    print("shortest trace:", " -> ".join(reversed(trace)) or "(initial)")
    print("state:", state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
