"""fae/conduct/_queues.py is the only code that names what lives in .queues/. A path
built anywhere else — `x / "work-slots"`, `f"cooldown.{agent}"` — is a second
owner of the same files, and the queues-lock no longer covers every change.

`offenders(root)` is the scan; an experiment's suite runs it on its own tree.
"""
import ast
import unittest
from pathlib import Path

from _ctx import ROOT, _EXPERIMENT

# Entries of .queues/ named as a path segment (`a / "queue"`).
SEGMENTS = {"queue", "running", "done", "backups", "work-slots"}
# Names and name prefixes that only .queues/ uses, in any string.
NAMES = (".queues", "work-slots", "weekly.json", "agent-io.json", "queues-lock")
PREFIXES = ("arm-", "slot-", "cooldown.")
OWNER = "_queues.py"
# fae/plane.py says where .queues/ is, and nothing about what is in it.
LOCATION = {"plane.py": {".queues"}}


def _leading(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return None


def offenders(root):
    """[(file, line, text)] of every .queues name outside fae/conduct/_queues.py."""
    out = []
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts or p.name == OWNER and p.parent.name == "conduct":
            continue
        allowed = LOCATION.get(p.name, set()) if p.parent.name == "fae" else set()
        tree = ast.parse(p.read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div):
                s = _leading(n.right)
                if s is not None and (s in SEGMENTS or s.startswith(PREFIXES) or s in NAMES) \
                        and s not in allowed:
                    out.append((str(p), n.lineno, s))
            elif isinstance(n, (ast.Constant, ast.JoinedStr)) and id(n) not in docs:
                s = _leading(n)
                if s is not None and s in NAMES and s not in allowed:
                    out.append((str(p), n.lineno, s))
    return sorted(set(out))


class TestOnlyQueuesNamesTheQueues(unittest.TestCase):

    def test_the_engine(self):
        self.assertEqual(offenders(Path(ROOT) / "fae"), [])

    def test_the_experiment_under_test(self):
        self.assertEqual(offenders(_EXPERIMENT), [])

    def test_the_scan_sees_a_second_owner(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.py").write_text(
                'from pathlib import Path\n'
                'a = Path("p") / "work-slots"\n'
                'b = Path("p") / f"cooldown.{1}"\n'
                'c = Path("p") / "queue" / "aaa"\n'
                'd = "weekly.json"\n')
            self.assertEqual([o[2] for o in offenders(d)],
                             ["work-slots", "cooldown.", "queue", "weekly.json"])


if __name__ == "__main__":
    unittest.main()
