"""Renumbering a lane restarts its sequence with the admission order intact,
however many specs it holds."""
import tempfile
import unittest
from pathlib import Path

from _ctx import runs


class TestRenumberKeepsAdmissionOrder(unittest.TestCase):

    def test_a_lane_longer_than_ten_keeps_its_order(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        cids = [f"m_high_beta_apidocs_T1_r{i}" for i in range(1, 24)]
        for i, cid in enumerate(cids):
            (d / f"{runs.queues.SEQ_START - 5 + i:06d}.{cid}.json").write_text("{}")
        runs.queues._renumber(d)
        after = runs.queues.specs_in(d)
        self.assertEqual([runs.queues.spec_cid(p) for p in after], cids)
        self.assertEqual(after[0].name.split(".", 1)[0], f"{runs.queues.SEQ_START:06d}")


if __name__ == "__main__":
    unittest.main()
