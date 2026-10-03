"""fae/cell is the only code that names a cell's own files, and Cell the only
writer of transitions.log, by appending. A path built anywhere else —
`ws / ".paused"`, `ledger.parse(ws)` — is a second reader or writer of the
same files that bypasses the cell's lock and its format.

`offenders(root, engine=...)` is the scan; an experiment's suite runs it on
its own tree.
"""
import ast
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT, _EXPERIMENT

# The cell's identity, ledger and markers, as they are named in its folder.
NAMES = {"cell.env", "iterations.log", ".sealed", ".paused", ".cancelled",
         "reconcile.flagged", ".loop"}
# fae/ledger.py's functions that open a workspace's ledger.
LEDGER_IO = {"parse", "append", "record_iter", "alert"}
# Calls that write or replace a file.
WRITES = {"write_text", "write_bytes", "rename", "replace", "unlink", "touch"}


def _leading(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return None


def _owners(root, engine):
    """Files allowed to name the cell's files: the cell package, the ledger's
    own format module, and the TLA+ checker fae ships (a standalone tool that
    replays a workspace it is pointed at)."""
    if not engine:
        return set()
    base = Path(root)
    return {p for p in base.rglob("*.py") if p.relative_to(base).parts[0] in ("cell", "utils")} \
        | {base / "ledger.py"}


def _about_transitions(node):
    return "transitions" in ast.unparse(node).lower()


def _write_mode(call, at):
    """Does `call` open for writing? Its mode is positional argument `at`."""
    mode = call.args[at] if len(call.args) > at else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
        and any(c in mode.value for c in "wax+")


def offenders(root, engine=False):
    """[(file, line, what)] of every cell file named outside fae/cell, every
    ledger opened outside it, and every write to transitions.log that is not
    Cell's append."""
    out = []
    owners = _owners(root, engine)
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts or "tests" in p.parts:
            continue
        tree = ast.parse(p.read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if p not in owners:
                if isinstance(n, (ast.Constant, ast.JoinedStr)) and id(n) not in docs \
                        and _leading(n) in NAMES:
                    out.append((str(p), n.lineno, _leading(n)))
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr in LEDGER_IO and ast.unparse(n.func.value).endswith("ledger"):
                    out.append((str(p), n.lineno, f"ledger.{n.func.attr}"))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                target = n.func.value
                if n.func.attr in WRITES and _about_transitions(target):
                    out.append((str(p), n.lineno, f"transitions .{n.func.attr}"))
                elif n.func.attr == "open" and _about_transitions(target) and _write_mode(n, 0):
                    out.append((str(p), n.lineno, "transitions .open"))
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open" \
                    and n.args and _about_transitions(n.args[0]) and _write_mode(n, 1):
                out.append((str(p), n.lineno, "transitions open"))
    return sorted(set(out))


class TestOnlyTheCellNamesItsFiles(unittest.TestCase):

    def test_the_engine(self):
        self.assertEqual(offenders(Path(ROOT) / "fae", engine=True), [])

    def test_the_experiment_under_test(self):
        self.assertEqual(offenders(_EXPERIMENT), [])

    def test_the_scan_sees_a_second_owner(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text(
                'from pathlib import Path\n'
                'from fae import ledger\n'
                'ws = Path("w")\n'
                'a = (ws / ".paused").exists()\n'
                'b = ledger.parse(ws)\n'
                'TRANSITIONS_LOG = Path("t")\n'
                'TRANSITIONS_LOG.open("w")\n'
                'TRANSITIONS_LOG.rename("old")\n'
                'with open(TRANSITIONS_LOG, "a") as f: pass\n'
                'TRANSITIONS_LOG.read_text()\n')
            self.assertEqual([o[2] for o in offenders(d)],
                             [".paused", "ledger.parse", "transitions .open",
                              "transitions .rename", "transitions open"])


if __name__ == "__main__":
    unittest.main()
