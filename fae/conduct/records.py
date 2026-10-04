"""The Conduct's own record in .conduct/: the reconcile log every action is
written to."""
from __future__ import annotations

import time

from fae import shared as _shared


def reconcile_log():
    return _shared.workspace().conduct / "reconcile.log"


def rec_log(msg):
    """Print `msg` and append it, stamped, to the reconcile log."""
    log = reconcile_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  {msg}"
    print(line)
    with log.open("a") as f:
        f.write(line + "\n")
