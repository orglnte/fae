"""What a workspace's cells produced since a moment: the cells sealed at or
after it, grouped by variant (green rate, iterations-to-green), and the cells
still working. Verdicts come through Cell's ledger (`fae/cell/ledger.py`, the
one parser), so this report cannot disagree with the scoreboard about what a
cell did.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from fae import host


def _epoch(stamp: str | None) -> float:
    """A seal's UTC stamp as epoch seconds; 0 for a seal that carries none."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


def collect(workspace, gate_n, since: float | None = None) -> tuple[dict, list]:
    """(completed_by_variant, in_progress) over `workspace`'s cells. A cell is
    completed if it is sealed at or after `since` (every sealed cell when
    `since` is None); in progress if its loop's heartbeat is live and it is
    not yet sealed."""
    done: dict = defaultdict(lambda: {"done": 0, "green": 0, "budget": 0, "itg": []})
    live: list = []
    for cid in workspace.cells():
        agent, variant, _task, rep = workspace.parse(cid)
        c = workspace.cell(cid)
        if c.heartbeat() is not None and not c.sealed:
            L = c.read_ledger(gate_n=gate_n)
            hb = host.heartbeat(c.ws, c)
            live.append((agent, variant, int(rep), L["att"],
                         (hb.get("phase") if hb else "") or "?"))
            continue
        if not c.sealed:
            continue
        if since is not None and _epoch(c.sealed_at) < since:
            continue
        L = c.read_ledger(gate_n=gate_n)
        b = done[variant]
        b["done"] += 1
        if L["verdict"] == "green":
            b["green"] += 1
            if L["green_at"]:
                b["itg"].append(L["green_at"])
        else:
            b["budget"] += 1
    return dict(done), live
