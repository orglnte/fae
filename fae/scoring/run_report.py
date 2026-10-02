"""What the CURRENT run produced: completed cells grouped by variant (green
rate + mean iterations-to-green) and the cells still working.

The run boundary is `conduct.pid`'s mtime — the moment the live scheduler
started — so "this run" means "since conduct last came up", overridable with
`--since`. Verdicts come through `fae/ledger.py`, the one ledger parser, so
this report cannot disagree with the scoreboard about what a cell did.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone

from fae import ledger
from fae.driver import common, state


def run_start() -> float | None:
    """Epoch of the current run's start: conduct.pid's mtime. None if no
    conduct has run (then the caller reports over all terminal cells)."""
    try:
        return (common.ORCH / "conduct.pid").stat().st_mtime
    except OSError:
        return None


def _parse_since(since: str | None) -> float | None:
    if since is None:
        return run_start()
    try:
        return float(since)                       # epoch seconds
    except ValueError:
        return datetime.fromisoformat(since).timestamp()   # ISO 8601


def collect(since: float | None) -> tuple[dict, list]:
    """(completed_by_variant, in_progress). A cell is completed-this-run if it
    is sealed and its seal landed at/after `since`; in-progress if a live loop
    marker is present and it is not yet sealed."""
    gate_n = common.definition().gate.arity
    done: dict = defaultdict(lambda: {"done": 0, "green": 0, "budget": 0, "itg": []})
    live: list = []
    for d in sorted(common.WS.iterdir()):
        if not d.is_dir():
            continue
        p = common.parse_cell_id(d.name)
        if not p:
            continue
        agent, variant, _task, rep = p
        sealed, loop = d / ".sealed", d / ".loop"
        if loop.exists() and not sealed.exists():
            L = ledger.parse(d, gate_n=gate_n)
            hb = state.heartbeat(d)
            live.append((agent, variant, int(rep), L["att"],
                         (hb.get("phase") if hb else "") or "?"))
            continue
        if not sealed.exists():
            continue
        if since is not None and sealed.stat().st_mtime < since:
            continue
        L = ledger.parse(d, gate_n=gate_n)
        b = done[variant]
        b["done"] += 1
        if L["verdict"] == "green":
            b["green"] += 1
            if L["green_at"]:
                b["itg"].append(L["green_at"])
        else:
            b["budget"] += 1
    return done, live


def _fmt(rows: list, header: tuple) -> str:
    cols = list(zip(*([header] + rows))) if rows else [(h,) for h in header]
    widths = [max(len(str(c)) for c in col) for col in cols]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths))
    return "\n".join([line(header)] + [line(r) for r in rows])


def report(since: str | None = None) -> str:
    since_ts = _parse_since(since)
    done, live = collect(since_ts)
    when = (datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            if since_ts else "all time (no conduct run found)")
    out = [f"— COMPLETED THIS RUN (since {when}) —"]
    rows, tot_d, tot_g, tot_b = [], 0, 0, 0
    for trt in sorted(done, key=lambda t: -done[t]["done"]):
        b = done[trt]
        itg = f"{sum(b['itg']) / len(b['itg']):.1f}" if b["itg"] else "-"
        rows.append((trt, b["done"], b["green"], b["budget"], itg))
        tot_d += b["done"]; tot_g += b["green"]; tot_b += b["budget"]
    out.append(_fmt(rows, ("VARIANT", "DONE", "GREEN", "BUDGET", "MEAN-ITG"))
               if rows else "  (none)")
    out.append(f"  total: {tot_d} done ({tot_g} green, {tot_b} budget)")
    out.append(f"\n— IN PROGRESS ({len(live)}) —")
    lrows = [(m, t, f"r{r}", f"{a}/{common.ATTEMPT_BUDGET}", ph)
             for m, t, r, a, ph in sorted(live)]
    out.append(_fmt(lrows, ("AGENT", "VARIANT", "REP", "ATTEMPT", "PHASE"))
               if lrows else "  (none)")
    return "\n".join(out)


def cli(since: str | None = None) -> int:
    print(report(since))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli(sys.argv[1] if len(sys.argv) > 1 else None))
