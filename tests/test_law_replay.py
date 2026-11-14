"""tools/law_replay.py re-judges archived arrangements with the law's
switches off and on and counts what changes, writing nothing."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT

import test_elastic_law as T

sys.path.insert(0, str(Path(ROOT) / "tools"))
import law_replay  # noqa: E402


def arrangement(d, rows, sched, variant="v1", ok=False, stage="scaling", numbered=False):
    d.mkdir(parents=True)
    with (d / "trace.csv").open("w") as f:
        f.write("epoch,ts_s,p99_ms,infra_cache_up\n" if numbered else "epoch,p99_ms,infra_cache_up\n")
        for r in rows:
            mid = f"{r['ts']}," if numbered else ""
            f.write(f"{5000.25 + r['ts']},{mid}{r['p99']},{r['up']}\n")
    events = {"start_epoch": 5000.0} if numbered else {"k6_start_epoch": 5000.25}
    (d / "trace_events.json").write_text(json.dumps(events))
    (d / "schedule.json").write_text(json.dumps(sched))
    (d / "metrics.json").write_text(json.dumps({"variant": variant, "load_total": 6000,
                                                "load_errors": 0}))
    (d / "verdict.json").write_text(json.dumps({"ok": ok, "stage": "" if ok else stage}))
    return d


class TestTheReplay(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        late = T.rows(100, saturated_from=14, mounted=[(46, 70)])      # 2s past the deadline
        self.dirs = [
            arrangement(self.base / "a" / "01-a1-S-charged", late, T.SCHED),
            arrangement(self.base / "b" / "01-a1-S-charged", late, T.SCHED, numbered=True,
                        variant="v2"),
            arrangement(self.base / "c" / "01-a1-S-green",
                        T.rows(100, saturated_from=14, mounted=[(22, 70)]), T.SCHED, ok=True),
            arrangement(self.base / "d" / "01-a1-S-charged", late, T.SCHED, stage="e2e"),
        ]
        (self.base / "e").mkdir()
        self.dirs.append(self.base / "e")

    def run_it(self, *opts):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            law_replay.main([*opts, *map(str, self.dirs)])
        return buf.getvalue().splitlines()

    def snapshot(self):
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.base.rglob("*")}

    def test_off_and_on_alike_is_no_change(self):
        out = self.run_it("--law-stages", "scaling,load-shape")
        self.assertIn("total\t3\t3\t", out)

    def test_a_tolerance_turns_a_near_miss_green_and_counts_it(self):
        before = self.snapshot()
        out = self.run_it("--law-stages", "scaling,load-shape", "--tolerance", "2", "--list")
        self.assertEqual(before, self.snapshot())
        self.assertIn("variant\tcounted\toff_agrees\tfalse->true", out)
        self.assertIn("v1\t2\t2\t1", out)
        self.assertIn("v2\t1\t1\t1", out)
        self.assertIn("skipped\t1\tdecided before the law", out)
        self.assertIn("skipped\t1\tfiles missing", out)
        listed = [l for l in out if "\tfalse->true\t" in l]
        self.assertEqual(len(listed), 2)
        self.assertIn("mount 1 at ts=46 outside", listed[0])

    def test_a_variants_own_tolerance_wins(self):
        out = self.run_it("--tolerance", "2", "--tolerance", "v2=1")
        self.assertIn("v2\t1\t1\t0", out)

    def test_a_tolerance_too_small_changes_nothing(self):
        out = self.run_it("--law-stages", "scaling", "--tolerance", "1")
        self.assertIn("total\t3\t3\t", out)


if __name__ == "__main__":
    unittest.main()
