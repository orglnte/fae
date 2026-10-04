"""fae/cell is the only code that names a cell's own files, and Cell the only
writer of transitions.log, by appending. A path built anywhere else —
`ws / ".paused"`, `ws / "verify.log"`, `ledger.parse(ws)`, an import of the
ledger module — is a second reader or writer of the same files that
bypasses the cell's lock and its format. A name handed to Cell's own
accessors (`cell.evidence("trace.csv")`) is the route, not a bypass.

`offenders(root, engine=..., producers=...)` is the scan; an experiment's
suite runs it on its own tree, naming its producers: the verifier, which
writes the verify's outputs into its own directory, and the infra classes,
which are the Cell's parts.
"""
import ast
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT, _EXPERIMENT

# Every file of the cell's folder, as it is named there: identity, ledger and
# markers; the recorded result and what is derived from it; the verify's
# outputs and logs; the seed and the run archive; the infra's own files.
NAMES = {"cell.env", "iterations.log", ".sealed", ".paused", ".cancelled",
         "reconcile.flagged", ".loop",
         "metrics.json", "validation.json", "score.json", "score-cache.json",
         "verify.log", "deploy.log", "service.deploy.log", "hooks.log", "run_cell.log",
         "trace.csv", "trace_events.json", "resources.json", "schedule.json",
         "PROMPT.md", ".skeleton_manifest", ".attempts.git", "arrangements", ".verify-out",
         "sarp.env", "pulumi.env", "kube.env", ".platformd-config"}
# Name prefixes only the cell's folder uses.
PREFIXES = ("agent.attempt-",)
# Cell's accessors that take a file's name: a name passed to one is read
# through the Cell.
CELL_ACCESS = {"evidence", "evidence_text", "evidence_all", "judged", "read_derived",
               "write_derived"}
# fae/cell/ledger.py's functions that open a workspace's ledger.
LEDGER_IO = {"parse", "append", "record_iter", "alert"}
# Calls that write or replace a file.
WRITES = {"write_text", "write_bytes", "rename", "replace", "unlink", "touch"}


def _leading(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return None


def _owners(root, engine, producers):
    """Files allowed to name the cell's files: the cell package (the ledger's
    format module included) for the engine; the `producers` (top-level
    directories under `root`) for an experiment."""
    base = Path(root)
    dirs = ("cell",) if engine else tuple(producers)
    return {p for p in base.rglob("*.py") if p.relative_to(base).parts[0] in dirs}


def _names_a_cell_file(s):
    return s in NAMES or s.startswith(PREFIXES)


def _through_cell(tree):
    """ids of the string constants handed to one of Cell's accessors."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in CELL_ACCESS and n.args and isinstance(n.args[0], ast.Constant):
            out.add(id(n.args[0]))
    return out


def _imports_ledger(node):
    """Does `node` import the ledger module, under any name?"""
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[-1] == "ledger" for a in node.names)
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[-1] == "ledger" \
            or any(a.name == "ledger" for a in node.names)
    return False


def _about_transitions(node):
    return "transitions" in ast.unparse(node).lower()


def _write_mode(call, at):
    """Does `call` open for writing? Its mode is positional argument `at`."""
    mode = call.args[at] if len(call.args) > at else next(
        (k.value for k in call.keywords if k.arg == "mode"), None)
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
        and any(c in mode.value for c in "wax+")


def offenders(root, engine=False, producers=()):
    """[(file, line, what)] of every cell file named outside its owners, every
    ledger opened outside fae/cell, and every write to transitions.log that is
    not Cell's append."""
    out = []
    owners = _owners(root, engine, producers)
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts or "tests" in p.parts:
            continue
        tree = ast.parse(p.read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        routed = _through_cell(tree)
        for n in ast.walk(tree):
            if p not in owners:
                s = _leading(n) if isinstance(n, (ast.Constant, ast.JoinedStr)) else None
                if s is not None and id(n) not in docs and id(n) not in routed \
                        and _names_a_cell_file(s):
                    out.append((str(p), n.lineno, s))
                elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                        and n.func.attr in LEDGER_IO and ast.unparse(n.func.value).endswith("ledger"):
                    out.append((str(p), n.lineno, f"ledger.{n.func.attr}"))
                elif _imports_ledger(n):
                    out.append((str(p), n.lineno, "import ledger"))
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
        self.assertEqual(offenders(_EXPERIMENT, producers=("verifier", "variants")), [])

    def test_a_name_handed_to_the_cell_is_not_a_bypass(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text(
                'def f(cell, ws):\n'
                '    a = cell.evidence("trace.csv")\n'
                '    b = cell.read_derived("score.json")\n'
                '    c = ws / "trace.csv"\n'
                '    e = ws.glob(f"agent.attempt-{1}.log")\n')
            self.assertEqual([(o[1], o[2]) for o in offenders(d)],
                             [(4, "trace.csv"), (5, "agent.attempt-")])

    def test_a_producer_may_name_what_it_writes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "verifier").mkdir()
            (Path(d) / "verifier" / "v.py").write_text('LOG = "verify.log"\n')
            self.assertEqual(offenders(d), [(str(Path(d) / "verifier" / "v.py"), 1, "verify.log")])
            self.assertEqual(offenders(d, producers=("verifier",)), [])

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
                             ["import ledger", ".paused", "ledger.parse", "transitions .open",
                              "transitions .rename", "transitions open"])

    def test_the_scan_sees_every_way_of_importing_the_ledger(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text(
                'from fae.cell import ledger\n'
                'from fae.cell.ledger import parse\n'
                'import fae.cell.ledger\n'
                'import ledger\n')
            self.assertEqual([o[1] for o in offenders(d)], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
