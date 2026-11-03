"""The host-load recorder: what it parses, whom it attributes a container
to, and the per-arrangement summary it writes."""
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from _ctx import ROOT  # noqa: F401  (sys.path)
from fae.conduct import hostload as H

TOP = """Processes: 600 total
Load Avg: 9.10, 8.00, 7.00
CPU usage: 50.0% user, 20.0% sys, 30.0% idle
Processes: 601 total
Load Avg: 4.25, 8.00, 7.00
CPU usage: 10.5% user, 5.5% sys, 84.0% idle
"""

STATS = ("fae-agent-m_high_v_access_T1_r1\t55.20%\t512.5MiB / 2GiB\n"
         "fae-verify-m_high_v_sealed_T1_r2\t101.00%\t1.5GiB / 4GiB\n"
         "ka-123-control-plane\t40.00%\t900MiB / 16GiB\n"
         "someone-else\t3.00%\t10MiB / 16GiB\n"
         "garbage line\n")

CIDS = ["m_high_v_access_T1_r1", "m_high_v_sealed_T1_r2"]


class TestParsing(unittest.TestCase):
    def test_top_reads_the_last_sample_not_the_since_boot_one(self):
        self.assertEqual(H.parse_top(TOP), {"cpu_user": 10.5, "cpu_sys": 5.5,
                                            "cpu_idle": 84.0, "load1": 4.25})

    def test_stats_rows_and_units(self):
        rows = H.parse_stats(STATS)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], ("fae-agent-m_high_v_access_T1_r1", 55.2, 512.5))
        self.assertEqual(rows[1][2], 1500.0)


class TestOwners(unittest.TestCase):
    def test_a_container_named_after_a_cell_is_that_cells(self):
        self.assertEqual(H.owner_of("fae-agent-m_high_v_access_T1_r1", CIDS, {}),
                         ("m_high_v_access_T1_r1", "fae-agent"))

    def test_a_kind_node_belongs_to_the_cell_owning_its_cluster(self):
        self.assertEqual(H.owner_of("ka-123-control-plane", CIDS, {"ka-123": CIDS[0]}),
                         (CIDS[0], "kind"))
        self.assertEqual(H.owner_of("ka-999-worker2", CIDS, {}), ("", "kind"))

    def test_anything_else_is_unowned(self):
        self.assertEqual(H.owner_of("someone-else", CIDS, {}), ("", "other"))


def raw(ts, idle, load, own, other, unowned):
    return [dict(ts=ts, scope="host", name="cpu_idle", owner="", role="", cpu_pct=str(idle), mem_mb=""),
            dict(ts=ts, scope="host", name="load1", owner="", role="", cpu_pct=str(load), mem_mb=""),
            dict(ts=ts, scope="container", name="v", owner=CIDS[1], role="fae-verify", cpu_pct=str(own), mem_mb=""),
            dict(ts=ts, scope="container", name="a", owner=CIDS[0], role="fae-agent", cpu_pct=str(other), mem_mb=""),
            dict(ts=ts, scope="container", name="k", owner="", role="kind", cpu_pct=str(unowned), mem_mb="")]


LEDGER = "\n".join([
    f"2026-11-03T10:00:00Z\tVERIFY_READY\t{CIDS[1]}\theld=10s",
    f"2026-11-03T10:00:30Z\tSHAPE\t{CIDS[1]}\tattempt=2\tBBS pass",
    f"2026-11-03T10:01:00Z\tVERIFY_READY\t{CIDS[1]}\theld=10s",
    f"2026-11-03T10:01:30Z\tSHAPE\t{CIDS[1]}\tattempt=2\tBSB FAIL (passed: BBS)",
])


class TestSummary(unittest.TestCase):
    def test_one_row_per_arrangement_with_its_own_window(self):
        rows = (raw("2026-11-03T10:00:10Z", 80, 3, 100, 10, 0) + raw("2026-11-03T10:00:20Z", 70, 4, 100, 20, 0)
                + raw("2026-11-03T10:01:10Z", 20, 9, 100, 150, 40) + raw("2026-11-03T10:01:20Z", 10, 8, 100, 170, 60))
        got = H.summarize(CIDS[1], 2, rows, LEDGER)
        self.assertEqual([(r["arrangement"], r["outcome"], r["samples"]) for r in got],
                         [("BBS", "pass", 2), ("BSB", "FAIL", 2)])
        self.assertEqual((got[0]["idle_min"], got[0]["idle_median"], got[0]["load1_max"]), (70.0, 75.0, 4.0))
        self.assertEqual(got[1]["own_cpu_median"], 100.0)
        self.assertEqual(got[1]["other_cells_cpu_median"], 160.0)
        self.assertEqual(got[1]["unowned_cpu_median"], 50.0)
        self.assertTrue(got[1]["top_other"].startswith(f"fae-agent:{CIDS[0]}="))

    def test_an_arrangement_outside_the_sampled_span_is_not_summarized(self):
        rows = raw("2026-11-03T10:01:10Z", 20, 9, 100, 150, 40)
        got = H.summarize(CIDS[1], 2, rows, LEDGER)
        self.assertEqual([r["arrangement"] for r in got], ["BSB"])


class TestRecorder(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.ws = SimpleNamespace(conduct=d, locks=d / "locks", cells=lambda: CIDS,
                                  cell=lambda cid: SimpleNamespace(ledger_text=lambda: LEDGER))
        self.rec = H.HostLoad(self.ws, run=lambda argv: TOP if argv[0] == "top" else STATS)

    def test_close_verify_appends_each_row_once(self):
        path = self.rec.raw_path(CIDS[1], 2)
        rows = raw("2026-11-03T10:00:10Z", 80, 3, 100, 10, 0) + raw("2026-11-03T10:01:10Z", 20, 9, 100, 150, 40)
        self.rec._append(path, H.RAW_FIELDS, rows)
        self.rec.close_verify(CIDS[1], 2)
        self.rec.close_verify(CIDS[1], 2)
        lines = (self.rec.dir / "summary.csv").read_text().splitlines()
        self.assertEqual(len(lines), 3)       # header + BBS + BSB

    def test_a_sample_attributes_every_container(self):
        with mock.patch.object(H.zombies, "_cluster_map", lambda: {CIDS[0]: "ka-123"}):
            got = self.rec.sample()
        owners = {r["name"]: r["owner"] for r in got if r["scope"] == "container"}
        self.assertEqual(owners["ka-123-control-plane"], CIDS[0])
        self.assertEqual(owners["someone-else"], "")
        self.assertIn("cpu_idle", {r["name"] for r in got if r["scope"] == "host"})


if __name__ == "__main__":
    unittest.main()
