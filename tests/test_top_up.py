"""top-up: fill missing reps to a target, rep-outer, without double-queueing.

Every test patches runs.common.WS and the scheduling plane to a TemporaryDirectory. Nothing here
starts a process: top_up only writes queue specs and never calls
conduct.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _ctx import runs, OrchTmpCase


def ns(**kw):
    base = dict(agent="sonnet", to_rep=3, variants=[], task="T1",
                budget=10, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


class TopUpTestCase(OrchTmpCase):
    # WS and plane temp-tree patching is inherited from OrchTmpCase.
    def specs(self, agent="sonnet"):
        return [runs.queues.read_spec(p) for p in runs.queues.lane_specs(agent)]


class TestSkipRules(TopUpTestCase):
    def cell(self, name, attempts=1, running=False):
        """A workspace that has RUN, unless told otherwise."""
        d = self.ws / name
        d.mkdir()
        ts = "2026-08-14T09:00:00Z"
        (d / "iterations.log").write_text(
            "".join(f"{ts}\tITER\tfail\tattempt={i} stage=scaling\n"
                    for i in range(1, attempts + 1)))
        if running:
            (d / ".loop").write_text("pid=1 cid=x phase=agent attempt=1 ts=1\n")
        return d

    def test_existing_workspace_is_skipped_whatever_its_verdict(self):
        self.cell("sonnet_high_beta_apidocs_T1_r2")
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        reps = [s["rep"] for s in self.specs()]
        self.assertEqual(reps, [1, 3])

    def test_workspace_rep_compares_as_a_number_not_a_string(self):
        """parse_cell_id returns the rep as a string; an unconverted set made
        every existing rep invisible and the top-up re-queued whole variants."""
        self.cell("sonnet_high_beta_apidocs_T1_r10")
        runs.ops.top_up(ns(to_rep=10, variants=["beta_apidocs"]))
        self.assertNotIn(10, [s["rep"] for s in self.specs()])

    def test_a_prepared_workspace_that_never_ran_is_still_missing(self):
        """It holds no attempt and will never be scored, so counting it caps
        the variant below target and every coverage report inherits that."""
        (self.ws / "sonnet_high_beta_apidocs_T1_r2").mkdir()
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 2, 3])

    def test_a_cell_running_its_first_attempt_counts(self):
        """No ITER line yet, but a live loop owns the rep — re-queueing it
        would put two loops on one workspace."""
        self.cell("sonnet_high_beta_apidocs_T1_r2",
                  attempts=0, running=True)
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 3])

    def test_already_queued_spec_is_skipped(self):
        runs.queues.enqueue("sonnet", {"task": "T1", "variant": "beta_apidocs", "rep": 1,
                                "budget": 10, "fresh": False})
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        reps = [s["rep"] for s in self.specs()]
        self.assertEqual(reps, [1, 2, 3])   # queued r1 + appended r2, r3

    def test_other_models_workspaces_do_not_count(self):
        (self.ws / "gemini_high_beta_apidocs_T1_r1").mkdir()
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 2, 3])


class TestOrderingAndScope(TopUpTestCase):
    def test_rep_outer_across_variants(self):
        """One rep of every variant, then the next rep — a partially drained
        queue must leave comparable n across the variants."""
        runs.ops.top_up(ns(variants=["alpha_apidocs", "beta_apidocs"], to_rep=2))
        got = [(s["rep"], s["variant"]) for s in self.specs()]
        self.assertEqual(got, [(1, "alpha_apidocs"), (1, "beta_apidocs"),
                               (2, "alpha_apidocs"), (2, "beta_apidocs")])

    def test_default_is_every_active_variant(self):
        runs.ops.top_up(ns(to_rep=1))
        self.assertEqual([s["variant"] for s in self.specs()],
                         list(runs.common.definition().active))

    def test_specs_are_never_fresh(self):
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        self.assertTrue(all(s["fresh"] is False for s in self.specs()))


class TestSafety(TopUpTestCase):
    def test_dry_run_writes_nothing(self):
        runs.ops.top_up(ns(dry_run=True))
        self.assertEqual(self.specs(), [])

    def test_existing_specs_are_untouched_by_an_append(self):
        """No backup dance: each spec is its own file, so appending cannot
        rewrite what is already queued."""
        runs.queues.enqueue("sonnet", {"task": "T1", "variant": "beta_howto", "rep": 9,
                                "budget": 10, "fresh": False})
        before = {p.name for p in runs.queues.lane_specs("sonnet")}
        runs.ops.top_up(ns(variants=["beta_apidocs"]))
        after = {p.name for p in runs.queues.lane_specs("sonnet")}
        self.assertTrue(before < after)

    def test_nothing_is_spawned(self):
        with mock.patch.object(runs.ops, "_spawn_detached") as sp:
            runs.ops.top_up(ns(variants=["beta_apidocs"]))
        sp.assert_not_called()

    def test_an_unknown_variant_refuses_loudly(self):
        with self.assertRaises(SystemExit):
            runs.ops.top_up(ns(variants=["beta_openbook"]))


if __name__ == "__main__":
    unittest.main()
