"""top-up: fill missing reps to a target, rep-outer, without double-queueing.

Every test patches runs.common.WS / runs.common.ORCH to a TemporaryDirectory. Nothing here
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
    base = dict(model="sonnet", to_rep=3, combos=[], task="T1",
                budget=10, dry_run=False)
    base.update(kw)
    return SimpleNamespace(**base)


class TopUpTestCase(OrchTmpCase):
    # WS/ORCH temp-tree patching is inherited from OrchTmpCase.
    def specs(self, model="sonnet"):
        return [runs.queue.read_spec(p) for p in runs.queue.lane_specs(model)]


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
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        reps = [s["rep"] for s in self.specs()]
        self.assertEqual(reps, [1, 3])

    def test_workspace_rep_compares_as_a_number_not_a_string(self):
        """parse_cell_id returns the rep as a string; an unconverted set made
        every existing rep invisible and the top-up re-queued whole arms."""
        self.cell("sonnet_high_beta_apidocs_T1_r10")
        runs.ops.top_up(ns(to_rep=10, combos=["beta·apidocs"]))
        self.assertNotIn(10, [s["rep"] for s in self.specs()])

    def test_a_prepared_workspace_that_never_ran_is_still_missing(self):
        """It holds no attempt and will never be scored, so counting it caps
        the combo below target and every coverage report inherits that."""
        (self.ws / "sonnet_high_beta_apidocs_T1_r2").mkdir()
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 2, 3])

    def test_a_cell_running_its_first_attempt_counts(self):
        """No ITER line yet, but a live loop owns the rep — re-queueing it
        would put two loops on one workspace."""
        self.cell("sonnet_high_beta_apidocs_T1_r2",
                  attempts=0, running=True)
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 3])

    def test_already_queued_spec_is_skipped(self):
        runs.queue.enqueue("sonnet", {"task": "T1", "treatment": "beta",
                                "condition": "apidocs", "rep": 1,
                                "budget": 10, "fresh": False})
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        reps = [s["rep"] for s in self.specs()]
        self.assertEqual(reps, [1, 2, 3])   # queued r1 + appended r2, r3

    def test_other_models_workspaces_do_not_count(self):
        (self.ws / "gemini_high_beta_apidocs_T1_r1").mkdir()
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        self.assertEqual([s["rep"] for s in self.specs()], [1, 2, 3])


class TestOrderingAndScope(TopUpTestCase):
    def test_rep_outer_across_combos(self):
        """One rep of every combo, then the next rep — a partially drained
        queue must leave comparable n across the matrix."""
        runs.ops.top_up(ns(combos=["alpha·apidocs", "beta·apidocs"],
                       to_rep=2))
        got = [(s["rep"], s["treatment"]) for s in self.specs()]
        self.assertEqual(got, [(1, "alpha"), (1, "beta"),
                               (2, "alpha"), (2, "beta")])

    def test_default_is_apidocs_on_every_treatment(self):
        runs.ops.top_up(ns(to_rep=1))
        combos = {(s["treatment"], s["condition"]) for s in self.specs()}
        self.assertEqual(combos, {(t, "apidocs") for t in runs.common.definition().matrix})

    def test_all_variants_is_the_whole_matrix(self):
        runs.ops.top_up(ns(to_rep=1, all_conditions=True))
        combos = {(s["treatment"], s["condition"]) for s in self.specs()}
        want = {(t, v) for t in runs.common.definition().matrix for v in runs.common.definition().matrix[t]}
        self.assertEqual(combos, want)

    def test_specs_are_never_fresh(self):
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        self.assertTrue(all(s["fresh"] is False for s in self.specs()))


class TestSafety(TopUpTestCase):
    def test_dry_run_writes_nothing(self):
        runs.ops.top_up(ns(dry_run=True))
        self.assertEqual(self.specs(), [])

    def test_existing_specs_are_untouched_by_an_append(self):
        """No backup dance: each spec is its own file, so appending cannot
        rewrite what is already queued."""
        runs.queue.enqueue("sonnet", {"task": "T1", "treatment": "beta",
                                "condition": "howto", "rep": 9,
                                "budget": 10, "fresh": False})
        before = {p.name for p in runs.queue.lane_specs("sonnet")}
        runs.ops.top_up(ns(combos=["beta·apidocs"]))
        after = {p.name for p in runs.queue.lane_specs("sonnet")}
        self.assertTrue(before < after)

    def test_nothing_is_spawned(self):
        with mock.patch.object(runs.ops, "_spawn_detached") as sp:
            runs.ops.top_up(ns(combos=["beta·apidocs"]))
        sp.assert_not_called()

    def test_unknown_combo_refuses_loudly(self):
        with self.assertRaises(SystemExit):
            runs.ops.top_up(ns(combos=["beta·openbook"]))


if __name__ == "__main__":
    unittest.main()


class TestTheDefaultSelectionIsApidocs(TopUpTestCase):
    """A bare top-up fills the apidocs batch and nothing else; other
    variants are named explicitly, or all of them at once."""

    def _selected(self, **kw):
        return sorted(runs.ops._default_combos(kw.get("conditions", []), kw.get("all_conditions", False)))

    def test_bare_is_apidocs_on_every_treatment(self):
        self.assertEqual(self._selected(), sorted((t, "apidocs") for t in runs.common.definition().matrix))

    def test_a_named_variant_is_added_where_a_treatment_has_it(self):
        got = self._selected(conditions=["howto"])
        self.assertIn(("alpha", "howto"), got)
        self.assertNotIn(("beta", "howto"), got)
        self.assertIn(("beta", "apidocs"), got)

    def test_all_variants_is_the_whole_matrix(self):
        self.assertEqual(self._selected(all_conditions=True),
                         sorted((t, v) for t in runs.common.definition().matrix for v in runs.common.definition().matrix[t]))

    def test_an_unknown_variant_is_refused(self):
        with self.assertRaises(SystemExit):
            self._selected(conditions=["openbok"])

    def test_top_up_enqueues_only_apidocs_by_default(self):
        runs.ops.top_up(ns(to_rep=1))
        self.assertEqual({s["condition"] for s in self.specs()}, {"apidocs"})
        self.assertEqual(len(self.specs()), len(runs.common.definition().matrix))

