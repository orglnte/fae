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



class TestTheHostFactsAreTheConducts(unittest.TestCase):
    """The host's facts are the Conduct's private module (fae/conduct/_host.py);
    the ones others read are exported by name from fae.conduct, and the
    Conduct class relays none of them."""

    def test_only_the_conduct_package_imports_its_host_module(self):
        pkg = Path(__file__).resolve().parents[1] / "fae"
        for f in sorted(pkg.rglob("*.py")):
            if "conduct" in f.relative_to(pkg).parts[:1]:
                continue
            self.assertIsNone(re.search(r"\b_host\b", f.read_text()), f.name)

    def test_the_host_facts_others_read_are_exported(self):
        import fae.conduct
        from fae.conduct import _host
        for name in ("agent_containers", "all_states", "cell_state", "containers", "heartbeat",
                     "loop_parents", "loop_pids", "mem_pressure", "queued"):
            self.assertIs(getattr(fae.conduct, name), getattr(_host, name), name)

    def test_the_conduct_relays_no_host_fact(self):
        from fae.conduct import Conduct
        for name in ("loop_parents", "loop_pids", "containers", "agent_containers",
                     "mem_pressure", "cell_state", "all_states", "heartbeat", "queued"):
            self.assertFalse(hasattr(Conduct, name), name)


if __name__ == "__main__":
    unittest.main()
