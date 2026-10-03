"""score-cache.json: per-cell incremental scoring cache.

Correctness contract: a warm run must produce the same score.json as a cold
one, recompute ONLY what changed, and never trust a cache written under
different scoring rules (CACHE_VERSION).
"""
import importlib.util as _ilu
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from _ctx import ROOT

_spec = _ilu.spec_from_file_location("sc", Path(ROOT) / "fae" / "scoring" / "score_cell.py")
sc = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(sc)

import tempfile


class CacheCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.art = self.ws / "artifacts"
        (self.art / "app").mkdir(parents=True)
        (self.art / "app" / "a.py").write_text("x = 1\n# c\n")
        (self.art / "app" / "b.py").write_text("y = 2\n\nz = 3\n")
        self.addCleanup(self._tmp.cleanup)

    def cell(self):
        from fae.cell.cell import Cell
        return Cell(self.ws.name, workspaces=self.ws.parent)

    def surface(self, cache):
        return sc.author_surface(self.art, cache)

    def _bump(self, p: Path, text):
        p.write_text(text)
        # mtime_ns granularity is fine, but same-ns rewrites exist in tests
        import os
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


class TestWarmEqualsCold(CacheCase):
    def test_second_run_returns_identical_surface(self):
        cache = {}
        cold = self.surface(cache)
        warm = self.surface(cache)
        self.assertEqual(cold, warm)

    def test_warm_run_rehashes_nothing(self):
        cache = {}
        self.surface(cache)
        with mock.patch.object(sc, "_sha256", side_effect=AssertionError("re-hash")) as m:
            self.surface(cache)

    def test_warm_run_skips_the_content_sniff_too(self):
        cache = {}
        self.surface(cache)
        with mock.patch.object(sc.surface_filter, "countable",
                               side_effect=AssertionError("re-sniff")):
            self.surface(cache)


class TestPartialInvalidation(CacheCase):
    def test_only_the_changed_file_is_rehashed(self):
        cache = {}
        self.surface(cache)
        self._bump(self.art / "app" / "a.py", "x = 9\n")
        hashed = []
        real = sc._sha256
        with mock.patch.object(sc, "_sha256",
                               side_effect=lambda p: (hashed.append(p.name), real(p))[1]):
            out = self.surface(cache)
        self.assertEqual(hashed, ["a.py"])
        row = {r["path"]: r for r in out["per_file"]}
        self.assertEqual(row["app/a.py"]["sloc"], 1)
        self.assertEqual(row["app/b.py"]["sloc"], 2)

    def test_deleted_file_leaves_the_surface(self):
        cache = {}
        self.surface(cache)
        (self.art / "app" / "b.py").unlink()
        out = self.surface(cache)
        self.assertEqual([r["path"] for r in out["per_file"]], ["app/a.py"])
        self.assertNotIn("app/b.py", cache["surface_files"])

    def test_cached_input_reuses_until_mtime_moves(self):
        f = self.ws / "metrics.json"
        f.write_text('{"A": 1}')
        cache = {}
        calls = []
        read = lambda: (calls.append(1), json.loads(f.read_text()))[1]
        self.assertEqual(sc.cached_input(cache, "m", f.stat().st_mtime_ns, read)["A"], 1)
        self.assertEqual(sc.cached_input(cache, "m", f.stat().st_mtime_ns, read)["A"], 1)
        self.assertEqual(len(calls), 1)
        self._bump(f, '{"A": 2}')
        self.assertEqual(sc.cached_input(cache, "m", f.stat().st_mtime_ns, read)["A"], 2)
        self.assertEqual(len(calls), 2)


class TestVersioning(CacheCase):
    def test_version_mismatch_discards_the_cache(self):
        (self.ws / "score-cache.json").write_text(json.dumps(
            {"version": sc.CACHE_VERSION - 1, "surface_files": {"poison": {}}}))
        self.assertEqual(sc.load_cache(self.cell()), {})

    def test_corrupt_cache_is_ignored(self):
        (self.ws / "score-cache.json").write_text("{not json")
        self.assertEqual(sc.load_cache(self.cell()), {})


if __name__ == "__main__":
    unittest.main()


class TestAgentTime(unittest.TestCase):
    """The agent's seconds per charged attempt come from the ledger's AGENT
    lines; a refunded attempt is not counted, and a re-run keeps its last run."""

    def ledger(self, *rows):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        log = Path(d.name) / "iterations.log"
        log.write_text("".join("2026-10-01T00:00:00Z\t" + "\t".join(r) + "\n" for r in rows))
        return sc.parse_agent_time(log.read_text())

    def test_charged_attempts_up_to_green_are_summed(self):
        t = self.ledger(("AGENT", "c", "attempt=1", "s=300"), ("ITER", "fail", "attempt=1 stage=e2e"),
                        ("AGENT", "c", "attempt=2", "s=100"), ("ITER", "green", "attempt=2 e2e=7/7"))
        self.assertEqual((t["agent_s_total"], t["agent_s_per_attempt"]), (400, 200.0))

    def test_a_refunded_attempt_is_not_counted_and_a_rerun_keeps_its_last_run(self):
        t = self.ledger(("AGENT", "c", "attempt=1", "s=900"),
                        ("AGENT", "c", "attempt=1", "s=120"), ("ITER", "green", "attempt=1"),
                        ("AGENT", "c", "attempt=2", "s=500"))
        self.assertEqual((t["agent_s_total"], t["agent_s_by_attempt"]), (120, {"1": 120}))

    def test_a_never_green_cell_sums_every_charged_attempt(self):
        t = self.ledger(("AGENT", "c", "attempt=1", "s=60"), ("ITER", "fail", "attempt=1"),
                        ("AGENT", "c", "attempt=2", "s=120"), ("ITER", "budget", "attempt=2"))
        self.assertEqual((t["agent_s_total"], t["agent_s_per_attempt"]), (180, 90.0))

    def test_a_charged_attempt_without_its_agent_line_leaves_the_totals_empty(self):
        t = self.ledger(("ITER", "fail", "attempt=1"),
                        ("AGENT", "c", "attempt=2", "s=120"), ("ITER", "green", "attempt=2"))
        self.assertEqual((t["agent_s_total"], t["agent_s_per_attempt"]), (None, None))
        self.assertEqual(t["agent_s_by_attempt"], {"2": 120})
