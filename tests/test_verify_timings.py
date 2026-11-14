"""fae_verify_timings.toml: one section per verify host, read and written
whole, refusing what it does not know."""
import tempfile
import tomllib
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401  (sys.path)

from fae.cell.verify_host import VerifyHost
from fae.cell.verify_timings import (FILE, HostTimings, Reference, TimingsError, VerifyTimings,
                                     host_key)

H1 = "ssh://operator@verify1"


def host(name=H1, refs=None, **kw):
    return HostTimings(host=name, date="2026-11-14T10:00:00Z", fae_commit="9d29b87",
                       experiment_commit="8c9598d", load={"LOAD_TOP_RPS": "266"},
                       probe={"store_qps": 96.0},
                       references=refs if refs is not None else (
                           Reference("pulumi_sealed_apidocs", "BSB",
                                     values={"sat_s": 6.6, "mount_s": 9.0},
                                     spreads={"sat_s": 1.0}),), **kw)


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, text):
        (self.root / FILE).write_text(text)

    def refused(self, text, why):
        self.write(text)
        with self.assertRaises(TimingsError) as ctx:
            VerifyTimings.read(self.root)
        self.assertIn(why, str(ctx.exception))


class TestRoundTrip(Case):
    def test_no_file_is_no_calibrated_host(self):
        self.assertEqual(VerifyTimings.read(self.root).hosts, {})

    def test_a_written_file_reads_back_the_same(self):
        VerifyTimings().with_host(host()).with_host(host("local", refs=())).write(self.root)
        got = VerifyTimings.read(self.root)
        self.assertEqual(got.host(H1), host())
        self.assertEqual(got.host("local"), host("local", refs=()))

    def test_the_file_is_plain_toml(self):
        VerifyTimings().with_host(host()).write(self.root)
        data = tomllib.loads((self.root / FILE).read_text())
        self.assertEqual(data["hosts"][H1]["probe"], {"store_qps": 96.0})
        self.assertEqual(data["hosts"][H1]["references"][0]["values"]["sat_s"], 6.6)

    def test_writing_one_host_keeps_the_others(self):
        VerifyTimings().with_host(host()).with_host(host("local")).write(self.root)
        t = VerifyTimings.read(self.root)
        t.with_host(host("local", refs=())).write(self.root)
        got = VerifyTimings.read(self.root)
        self.assertEqual(got.host(H1), host())
        self.assertEqual(got.host("local").references, ())

    def test_a_reference_is_found_by_variant_and_arrangement(self):
        h = host()
        self.assertEqual(h.reference("pulumi_sealed_apidocs", "BSB").values["mount_s"], 9.0)
        self.assertIsNone(h.reference("pulumi_sealed_apidocs", "SSB"))


class TestRefusals(Case):
    HEAD = f'[hosts."{H1}"]\ndate = "d"\nfae_commit = "a"\nexperiment_commit = "b"\n'
    REF = f'\n[[hosts."{H1}".references]]\nvariant = "v"\narrangement = "BSB"\n'

    def test_an_unknown_top_key(self):
        self.refused('top = 1\n', "unknown key(s) ['top']")

    def test_an_unknown_host_key(self):
        self.refused(self.HEAD + "tolerance = 1\n", "unknown key(s) ['tolerance']")

    def test_a_section_without_its_provenance(self):
        self.refused(f'[hosts."{H1}"]\ndate = "d"\n', "missing fae_commit, experiment_commit")

    def test_an_unknown_spread_kind(self):
        self.refused(self.HEAD + self.REF + 'spread = "guessed"\n', "spread is one of")

    def test_a_measured_spread_from_one_run(self):
        self.refused(self.HEAD + self.REF + 'spread = "measured"\nn = 1\n', "needs n >= 2")

    def test_a_spread_for_no_value(self):
        self.refused(self.HEAD + self.REF + 'values = { a = 1.0 }\nspreads = { b = 1.0 }\n',
                     "spreads for no value: ['b']")

    def test_the_same_reference_twice(self):
        self.refused(self.HEAD + self.REF + self.REF, "appears twice")

    def test_a_broken_file_names_itself(self):
        self.refused("[hosts\n", FILE)

    def test_a_non_finite_number_is_not_written(self):
        h = host(refs=(Reference("v", "BSB", values={"a": float("nan")}, spreads={}),))
        with self.assertRaises(TimingsError):
            VerifyTimings().with_host(h).write(self.root)
        self.assertFalse((self.root / FILE).exists())


class TestHostKey(unittest.TestCase):
    def test_a_remote_host_is_its_daemon_and_the_local_one_is_local(self):
        self.assertEqual(host_key(VerifyHost(H1)), H1)
        self.assertEqual(host_key(VerifyHost("")), "local")


if __name__ == "__main__":
    unittest.main()
