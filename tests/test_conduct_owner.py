"""Supervision is the Conduct's: the supervision pass (supervise.py) and the
leftovers' reaper (zombies.py) live in its package, and every other module
reaches them through the Conduct. A module importing them is a second
supervisor."""
import re
import unittest
from pathlib import Path

from _ctx import ROOT

PARTS = r"(supervise|zombies)"
IMPORTS = (re.compile(rf"from\s+fae\.conduct\s+import\s+[^\n]*\b{PARTS}\b"),
           re.compile(rf"\bfae\.conduct\.{PARTS}\b"))


def offenders(root):
    """[(file, line)] of every engine module (under `root`/fae) outside
    fae/conduct/ that imports one of its parts."""
    base = Path(root)
    out = []
    for f in sorted((base / "fae").rglob("*.py")):
        rel = f.relative_to(base).as_posix()
        if rel.startswith("fae/conduct/") or rel.startswith("fae/utils/"):
            continue
        for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if any(p.search(line.split("#", 1)[0]) for p in IMPORTS):
                out.append((rel, i))
    return out


class TestTheConductOwnsSupervision(unittest.TestCase):
    def test_the_engine(self):
        self.assertEqual(offenders(ROOT), [])

    def test_the_scan_sees_a_second_supervisor(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fae" / "conduct").mkdir(parents=True)
            (Path(d) / "fae" / "x.py").write_text("from fae.conduct import zombies\n")
            (Path(d) / "fae" / "conduct" / "y.py").write_text("from . import zombies\n")
            self.assertEqual(offenders(Path(d)), [("fae/x.py", 1)])


if __name__ == "__main__":
    unittest.main()
