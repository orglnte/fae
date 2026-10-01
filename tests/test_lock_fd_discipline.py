"""Source-text guards on the two rules that break mutual exclusion silently.

A flock is released when the LAST fd on its open file description closes, and
it belongs to the file's inode. So:

  1. anything that can outlive its driver must not inherit the lock fds, or a
     dead cell keeps its slot;
  2. no lock file may ever be unlinked, or two holders end up on two inodes.

Neither failure raises anything. Both look exactly like the stale locks this
mutex exists to abolish, so they are pinned here rather than left to review.

Rule 1 is now the language's default: subprocess closes fds unless told
otherwise, so the only way to hand a child the arena is an explicit
`pass_fds` / `close_fds=False` — and the cell package writes neither. The
bash side of this file (mutex_nolock, the fd arena drivers) retired with the
bash harness; tests/test_mutex_kernel.py keeps the negative control that an
inherited fd DOES pin a lock.
"""
import re
import unittest
from pathlib import Path

from _ctx import ROOT

HARNESS = Path(ROOT) / "fae"
PKG = HARNESS / "cell"
DRIVER = Path(ROOT) / "driver"

# Every source file the two scans below read: the harness (bash + Python),
# fae/driver/ (the arm/slot/verify-lock acquire-and-release code) and cli.py.
SCANNED_FILES = (list(HARNESS.rglob("*.sh")) + list(HARNESS.rglob("*.py"))
                 + list(DRIVER.glob("*.py")) + [Path(ROOT) / "fae" / "cli.py"])


def read(rel):
    return (HARNESS / rel).read_text()


class TestNoChildIsHandedTheArena(unittest.TestCase):
    """Every child a holder starts — the bring-up, the agent shell, k6, the
    daemons — runs under close_fds=True. One exception is named and fatal to
    the rule it would break."""

    def test_no_module_hands_fds_to_a_child(self):
        for f in sorted(PKG.glob("*.py")):
            body = f.read_text()
            for line in body.splitlines():
                s = line.split("#", 1)[0]
                self.assertNotIn("pass_fds=", s, f"{f.name}: {line.strip()}")
                self.assertNotIn("close_fds=False", s, f"{f.name}: {line.strip()}")

class TestTheOldProtocolIsGone(unittest.TestCase):
    """These names coming back means the reclamation machinery is back."""

    GONE = ["steal_if_stale", "mutex_steal_if_stale", "ORPHAN_GRACE_S",
            "DEHERD_MAX_S", "SETTLE_S", "ARM_LOCK_MAX_AGE"]

    def test_no_reclamation_symbols_remain(self):
        hits = []
        for path in SCANNED_FILES:
            try:
                body = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for name in self.GONE:
                if name in body:
                    hits.append(f"{path.name}: {name}")
        self.assertEqual(hits, [], f"the old protocol is creeping back: {hits}")


class TestNoLockPathIsEverRemoved(unittest.TestCase):
    """Unlink-and-recreate is the one silent total failure: two holders, two
    inodes, no error anywhere."""

    def test_no_source_removes_a_lock_file(self):
        bad = []
        for path in SCANNED_FILES:
            try:
                body = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            for line in body.splitlines():
                s = line.strip()
                if s.startswith("#"):
                    continue
                if not re.search(r"\b(rm -rf|rm -f|unlink|rmdir)\b", s):
                    continue
                # the holder SIDECAR is display-only and may be removed
                if ".holder" in s or "_holder_path" in s or "clear_holder" in s:
                    continue
                if re.search(r"(work-slots|arm-.*\.slots|verify-lock|rig-lock|"
                             r"loop-locks|LOCK_DIR|LOOP_LOCK)", s):
                    bad.append(f"{path.name}: {s}")
        self.assertEqual(bad, [], f"a lock path is being removed: {bad}")


if __name__ == "__main__":
    unittest.main()
