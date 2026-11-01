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


class TestReorder(unittest.TestCase):

    def test_a_lane_takes_the_keys_order_and_ties_keep_theirs(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(base, ignore_errors=True))
        from fae.conduct._queues import Queues
        q = Queues(base / ".queues", locks=base / ".locks")
        (base / ".locks").mkdir()
        lane = q.lane_dir("m")
        lane.mkdir(parents=True)
        cids = ["m_high_slow_a_T1_r1", "m_high_fast_a_T1_r1", "m_high_slow_b_T1_r1",
                "m_high_fast_b_T1_r1", "m_high_fast_a_T1_r2"]
        for i, cid in enumerate(cids):
            (lane / f"{runs.queues.SEQ_START + i:06d}.{cid}.json").write_text("{}")
        q.reorder("m", lambda cid: 0 if "_fast_" in cid else 1)
        self.assertEqual([q.spec_cid(p) for p in q.lane_specs("m")],
                         ["m_high_fast_a_T1_r1", "m_high_fast_b_T1_r1", "m_high_fast_a_T1_r2",
                          "m_high_slow_a_T1_r1", "m_high_slow_b_T1_r1"])
        self.assertEqual(q.lane_specs("m")[0].name.split(".", 1)[0], f"{runs.queues.SEQ_START:06d}")


if __name__ == "__main__":
    unittest.main()
