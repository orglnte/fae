"""The footprint sampler counts the measured cell's containers only, and
trusts a number only when two sources agree.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

_path = Path(ROOT) / "fae" / "cell" / "instruments" / "resource_sampler.py"
_spec = importlib.util.spec_from_file_location("_t_resource_sampler", _path)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

STATS = ("fae-store-x\t3.0%\t120MiB / 1GiB\n"
         "fae-dind-cell-a\t10.0%\t300MiB / 1GiB\n"
         "fae-dind-cell-b\t50.0%\t900MiB / 1GiB\n"
         "exp-ka-111-control-plane\t40.0%\t2000MiB / 4GiB\n"
         "exp-ka-222-control-plane\t40.0%\t2100MiB / 4GiB\n")


class TestTheSamplerIsScopedToTheCell(unittest.TestCase):

    def test_only_the_cells_own_containers_are_counted(self):
        own = {"fae-store-x", "fae-dind-cell-a", "exp-ka-111-control-plane"}
        with mock.patch.object(rs, "_run", lambda cmd, timeout=8.0: STATS):
            rows = rs.sample_docker(own)
        self.assertEqual(sorted(r["name"] for r in rows), sorted(own))
        tags = {r["name"]: r["tag"] for r in rows}
        self.assertEqual(tags["exp-ka-111-control-plane"], "substrate")
        self.assertEqual(tags["fae-dind-cell-a"], "app")

    def test_no_scope_means_the_whole_machine(self):
        with mock.patch.object(rs, "_run", lambda cmd, timeout=8.0: STATS):
            self.assertEqual(len(rs.sample_docker(None)), 5)


class TestEveryNumberHasTwoSources(unittest.TestCase):

    def rows(self):
        return [{"plane": "docker", "name": "fae-dind-x", "tag": "app", "cpu_pct": 1, "mem_mb": 300.0},
                {"plane": "docker", "name": "exp-ka-1-control-plane", "tag": "substrate",
                 "cpu_pct": 1, "mem_mb": 2000.0},
                {"plane": "k8s", "name": "ns-x/cache", "tag": "app", "cpu_pct": 1, "mem_mb": 50.0}]

    def test_agreeing_sources_are_reliable(self):
        cg = {"fae-dind-x": 310.0, "exp-ka-1-control-plane": 1900.0}
        ok, findings = rs.cross_check(self.rows(), 15.0, cgroup=cg.get)
        self.assertTrue(ok, findings)

    def test_a_disagreement_beyond_the_tolerance_is_named(self):
        cg = {"fae-dind-x": 600.0, "exp-ka-1-control-plane": 1900.0}
        ok, findings = rs.cross_check(self.rows(), 15.0, cgroup=cg.get)
        self.assertFalse(ok)
        self.assertEqual(findings, ["fae-dind-x: docker stats 300 MB vs cgroup 600 MB"])

    def test_an_unreadable_second_source_is_not_silently_trusted(self):
        ok, findings = rs.cross_check(self.rows(), 15.0, cgroup=lambda n: None)
        self.assertFalse(ok)
        self.assertEqual(len(findings), 2)

    def test_pods_cannot_exceed_their_node(self):
        rows = self.rows()
        rows[2]["mem_mb"] = 2500.0
        ok, findings = rs.cross_check(rows, 15.0, cgroup={"fae-dind-x": 300.0, "exp-ka-1-control-plane": 2000.0}.get)
        self.assertFalse(ok)
        self.assertIn("exceed their node", findings[0])


class TestTheSummaryCarriesTheTrustFlags(unittest.TestCase):

    def run_main(self, own, cgroup_mb):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        argv = ["resource_sampler", "--duration-s", "0.2", "--interval-s", "0.1",
                "--csv-output", str(d / "r.csv"), "--json-output", str(d / "r.json"),
                "--namespaces", "", "--substrate-namespaces", ""] + own
        with mock.patch.object(rs, "_run", lambda cmd, timeout=8.0: STATS if cmd[:2] == ["docker", "stats"] else ""), \
             mock.patch.object(rs, "_cgroup_mem_mb", lambda name: cgroup_mb), \
             mock.patch.object(rs.time, "sleep", lambda s: None):
            rs.main(argv)
        return json.loads((d / "r.json").read_text())

    def test_a_scoped_agreeing_run_is_reliable(self):
        s = self.run_main(["--own", "fae-store-x", "--own", "fae-dind-cell-a"], 120.0)
        # both containers report ~120 MB from the cgroup: the store agrees, the sidecar (300) does not
        self.assertEqual(s["scope"], "cell")
        self.assertFalse(s["reliable"])
        self.assertTrue(any("fae-dind-cell-a" in f for f in s["cross_check_findings"]))
        s2 = self.run_main(["--own", "fae-store-x"], 120.0)
        self.assertTrue(s2["reliable"])
        self.assertEqual(s2["peak"]["component_count"], 1)

    def test_a_machine_wide_run_is_never_reliable(self):
        s = self.run_main([], 120.0)
        self.assertEqual(s["scope"], "machine")
        self.assertFalse(s["reliable"])


class TestTheCgroupSourceUsesDockersDefinition(unittest.TestCase):
    """docker stats reports usage minus the inactive file cache; the second
    source must measure the same thing or a cache-heavy kind node reads
    2-3x its stats figure and every sample is "unreliable"."""

    def test_v2_subtracts_inactive_file(self):
        mb = rs.cgroup_mem_mb_from("3174400000\ninactive_file 1900000000\n")
        self.assertAlmostEqual(mb, (3174400000 - 1900000000) / 1048576, places=1)

    def test_v1_subtracts_total_inactive_file(self):
        mb = rs.cgroup_mem_mb_from("2097152\ntotal_inactive_file 1048576\n")
        self.assertAlmostEqual(mb, 1.0, places=3)

    def test_no_stat_line_means_the_raw_figure(self):
        self.assertAlmostEqual(rs.cgroup_mem_mb_from("1048576\n"), 1.0, places=3)

    def test_garbage_is_none(self):
        self.assertIsNone(rs.cgroup_mem_mb_from(""))
        self.assertIsNone(rs.cgroup_mem_mb_from("cat: no such file"))


if __name__ == "__main__":
    unittest.main()
