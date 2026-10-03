"""Supervision is the Conduct's: the supervision pass (supervise.py), the
leftovers' reaper (zombies.py) and the host's view of the fleet (host.py)
live in its package, and every other module reads them through the Conduct's
own methods. A module importing them is a second supervisor, or a reader that
bypasses the run's view."""
import re
import unittest
from pathlib import Path

from _ctx import ROOT

PARTS = r"(host|supervise|zombies)"
IMPORTS = (re.compile(rf"from\s+fae\.driver\.conduct\s+import\s+[^\n]*\b{PARTS}\b"),
           re.compile(rf"\bfae\.driver\.conduct\.{PARTS}\b"),
           re.compile(rf"from\s+fae\.driver\s+import\s+[^\n]*\b{PARTS}\b"))


def offenders(root):
    """[(file, line)] of every engine module (under `root`/fae) outside
    fae/driver/conduct/ that imports one of its parts."""
    base = Path(root)
    out = []
    for f in sorted((base / "fae").rglob("*.py")):
        rel = f.relative_to(base).as_posix()
        if rel.startswith("fae/driver/conduct/") or rel.startswith("fae/utils/"):
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
            (Path(d) / "fae" / "driver").mkdir(parents=True)
            (Path(d) / "fae" / "driver" / "x.py").write_text(
                "from fae.driver.conduct import zombies\n")
            (Path(d) / "fae" / "driver" / "conduct").mkdir()
            (Path(d) / "fae" / "driver" / "conduct" / "y.py").write_text(
                "from . import zombies\n")
            self.assertEqual(offenders(Path(d)), [("fae/driver/x.py", 1)])


if __name__ == "__main__":
    unittest.main()
