"""The per-run archive a verify leaves under <ws>/arrangements/: one folder per
run, `NN-a<attempt>-<shape>-<end state>` with a verdict.json, or `NN-<shape>`
for runs archived before end states were recorded (those were always judged
runs)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

JUDGED = ("green", "charged")
NOT_CHARGED = ("refunded", "interrupted")
_NAME = re.compile(r"^(\d+)-a(\d+)-(.+)-(green|charged|refunded|interrupted)$")
_LEGACY = re.compile(r"^(\d+)-(.+)$")


@dataclass(frozen=True)
class Run:
    path: Path
    attempt: int | None
    shape: str
    end_state: str | None       # None: a legacy folder, judged

    @property
    def judged(self):
        return self.end_state is None or self.end_state in JUDGED

    def verdict(self):
        try:
            return json.loads((self.path / "verdict.json").read_text())
        except (OSError, ValueError):
            return {}


def runs(ws):
    """Every archived run of the workspace, in archive order."""
    base = Path(ws) / "arrangements"
    if not base.is_dir():
        return []
    out = []
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        m = _NAME.match(d.name)
        if m:
            out.append(Run(d, int(m.group(2)), m.group(3), m.group(4)))
            continue
        m = _LEGACY.match(d.name)
        if m:
            out.append(Run(d, None, m.group(2), None))
    return out


def judged(ws):
    return [r for r in runs(ws) if r.judged]


def judged_files(ws, name):
    """`name` from every judged run that has it, in archive order."""
    return [r.path / name for r in judged(ws) if (r.path / name).exists()]
