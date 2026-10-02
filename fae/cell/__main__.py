"""Run one cell: python3 -m fae.cell TASK VARIANT [REP] [--stub DIR]

AGENT and EFFORT come from the environment; REFERENCE=1 seeds the variant's
known answer (a smoke cell). --stub DIR runs the rig-debug path: no agent,
DIR copied over the artifacts (an empty DIR verifies what prepare seeded,
e.g. the reference), one attempt, the full gate.
"""
import sys

from .cell import Cell, Halt
from .fsm import Sealed


def main(argv=None):
    a = list(argv if argv is not None else sys.argv[1:])
    stub = None
    if "--stub" in a:
        i = a.index("--stub")
        stub = a[i + 1] if i + 1 < len(a) else None
        del a[i:i + 2]
        if not stub:
            print("--stub needs a directory", file=sys.stderr)
            return 2
    if len(a) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    task, variant = a[0], a[1]
    rep = a[2] if len(a) > 2 else "1"
    # cell_id is encoded in ONE place; reimplementing the format here is the
    # drift that makes a cell write to one workspace and be read from another.
    import os
    from fae.driver import common
    cid = common.cell_id(os.environ.get("AGENT", "?"), variant, rep,
                         task, effort=os.environ.get("EFFORT", "high"),
                         smoke=bool(os.environ.get("SMOKE")))

    c = Cell(cid)
    c._env.setdefault("TASK", task)
    c._env.setdefault("VARIANT", variant)
    if os.environ.get("REFERENCE") == "1":
        c._env.setdefault("REFERENCE", "1")
    c._env.setdefault("REPEAT", str(rep))
    try:
        verdict = c.run(stub_overlay=stub)
    except Sealed as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 46
    except Halt as e:
        print(str(e), file=sys.stderr)
        return e.code
    except RuntimeError as e:
        # Anything not raised as Halt (which is caught above) is an
        # unclassified crash, not the benign lock-contention refusal.
        print(str(e), file=sys.stderr)
        return common.GENERIC_CRASH_EXIT_RC
    print(c.ws)
    return 0 if verdict in ("green", "failed", None) else 1


if __name__ == "__main__":
    sys.exit(main())
