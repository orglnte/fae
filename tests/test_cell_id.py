"""cell_id / parse_cell_id — the id format both languages must agree on.

The id has ONE implementation: the driver's entry point and the prepare
import `runs.cell_id` rather than re-encoding the format, and every operator
verb (status, pause, kill, reconcile) finds a cell by parsing its directory
name back apart. A format change that only one side knows about strands live
cells.
"""
import unittest

from _ctx import runs


class TestCellId(unittest.TestCase):

    def test_default_effort_is_high(self):
        self.assertEqual(
            runs.cell_id("sonnet", "beta_apidocs", 1),
            "sonnet_high_beta_apidocs_T1_r1")

    def test_task_and_rep_are_positional_suffixes(self):
        self.assertEqual(
            runs.cell_id("haiku", "alpha_openbook", 12, task="T3"),
            "haiku_high_alpha_openbook_T3_r12")

    def test_smoke_inserts_marker_before_the_variant(self):
        self.assertEqual(
            runs.cell_id("gemini", "beta_onlysrc", 2, smoke=True),
            "gemini_high_smoke_beta_onlysrc_T1_r2")

    def test_empty_effort_drops_the_suffix(self):
        """Mirrors config.sh's `${EFFORT:+_$EFFORT}`: empty means absent, not "_".

        Documented in the function's own comment as deliberate parity with
        config.sh's CELL_PREFIX. See TestParseCellId.test_KNOWN_FAIL_empty_effort_id_is_unparseable
        for the consequence.
        """
        self.assertEqual(
            runs.cell_id("sonnet", "beta_apidocs", 1, effort=""),
            "sonnet_beta_apidocs_T1_r1")


class TestParseCellId(unittest.TestCase):

    def test_parses_the_live_id_shape(self):
        self.assertEqual(
            runs.parse_cell_id("sonnet_high_beta_apidocs_T1_r1"),
            ("sonnet", "beta_apidocs", "T1", "1"))

    def test_round_trips_every_variant(self):
        for variant in runs.experiment.definition().ids:
            for agent in ("sonnet", "haiku", "gemini", "opus", "dsv4f", "kimi"):
                cid = runs.cell_id(agent, variant, 3)
                with self.subTest(cid=cid):
                    self.assertEqual(runs.parse_cell_id(cid), (agent, variant, "T1", "3"))

    def test_non_high_effort_still_parses(self):
        """Regression: a `_high_` hardcode once made every non-high cell
        invisible to status/pause/kill while its loop kept running."""
        cid = runs.cell_id("sonnet", "alpha_howto", 1, effort="medium")
        self.assertEqual(cid, "sonnet_high_alpha_howto_T1_r1".replace(
            "_high_", "_medium_"))
        self.assertEqual(runs.parse_cell_id(cid),
                         ("sonnet", "alpha_howto", "T1", "1"))

    def test_multi_digit_rep_and_task(self):
        self.assertEqual(
            runs.parse_cell_id("haiku_high_alpha_openbook_T3_r12"),
            ("haiku", "alpha_openbook", "T3", "12"))

    def test_hyphenated_model_name(self):
        self.assertEqual(
            runs.parse_cell_id("claude-opus-5_high_beta_onlysrc_T1_r1"),
            ("claude-opus-5", "beta_onlysrc", "T1", "1"))

    def test_rejects_an_unknown_variant(self):
        """An id whose variant the loaded experiment does not declare is not a cell
        of this experiment — another experiment's, or an archived vocabulary —
        and matching it would pull foreign cells into status and reconcile."""
        for cid in ("gemini_high_gamma_handed_T1_r1",
                    "gemini_high_other_arm_withheld_T1_r1",
                    "haiku_high_delta_handed_plus_T2_r1",
                    "haiku_high_gamma_maxhelp_T1_r1"):
            with self.subTest(cid=cid):
                self.assertIsNone(runs.parse_cell_id(cid))

    def test_rejects_malformed(self):
        for cid in ("", "not-a-cell", "sonnet_high_beta_apidocs_T1",
                    "sonnet_high_beta_apidocs_T1_rX",
                    "sonnet_high_beta_apidocs_r1"):
            with self.subTest(cid=cid):
                self.assertIsNone(runs.parse_cell_id(cid))

    def test_rejects_trailing_garbage(self):
        """Anchored at the end, so a suffixed directory (a backup copy, a
        `.bak`) is not mistaken for the cell it was copied from."""
        self.assertIsNone(
            runs.parse_cell_id("sonnet_high_beta_apidocs_T1_r1.bak"))

    def test_empty_effort_id_is_unparseable_BY_DECISION(self):
        """cell_id and parse_cell_id disagree when effort is "" — accepted.

        cell_id(effort="") yields `sonnet_beta_apidocs_T1_r1`, but the
        parse regex requires a mandatory `_(?:\\w+?)_` effort segment between
        agent and variant, so it returns None. Such a cell would run and then
        be invisible to status, pause, kill and reconcile.

        Not fixed by changing the derivation: that would touch every cid
        the orchestrator computes (conduct, spawn, queued_summary,
        _scrub_queues) in the middle of a live study. Operator decision
        2026-07-30 — fail LOUDLY instead, which `cli.py experiment check` now
        does whenever EFFORT is not "high" or SMOKE is set. This test pins
        the asymmetry so it stays a
        known, guarded property rather than a surprise.
        """
        cid = runs.cell_id("sonnet", "beta_apidocs", 1, effort="")
        self.assertIsNone(runs.parse_cell_id(cid))

    def test_the_check_refuses_a_non_high_effort(self):
        """The loud guard chosen instead of changing cid derivation."""
        import os
        from unittest import mock
        from fae.experiment import check_exp as check
        ctx = check.Ctx(root=runs.ROOT, static=True)
        with mock.patch.dict(os.environ, {"EFFORT": "medium"}):
            found = check._invariants(ctx)
        bad = [f for f in found if not f.ok]
        self.assertTrue(any("EFFORT='medium'" in f.text for f in bad), [f.text for f in found])


class TestOneImplementation(unittest.TestCase):
    """Every other producer of a cell id imports fae.experiment.cell_id;
    nothing shells out to a hidden subcommand."""

    def test_the_driver_and_the_prepare_import_the_function(self):
        for rel in ("fae/cell/__main__.py", "fae/cell/prepare.py"):
            self.assertIn("from fae.experiment import cell_id", (runs.ROOT / rel).read_text(), rel)

    def test_no_hidden_subcommand_remains(self):
        src = (runs.ROOT / "fae" / "cli" / "__init__.py").read_text()
        self.assertNotIn('add_parser("_cell_id")', src)
        self.assertNotIn('add_parser("_seed_doc")', src)
        self.assertNotIn('command("_cell_id")', src)
        self.assertNotIn('command("_seed_doc")', src)


if __name__ == "__main__":
    unittest.main()


class TestTheVariantIsTheExperiments(unittest.TestCase):
    """The grammar is positional (the variant is whatever lies between the
    effort and the last two tokens); the variant itself must be one the
    loaded experiment declares."""

    def test_an_undeclared_variant_is_not_a_cell(self):
        self.assertIsNone(runs.parse_cell_id("sonnet_high_gpu_sealed_apidocs_T1_r1"))
        self.assertIsNone(runs.parse_cell_id("sonnet_high_apidocs_T1_r1"))

    def test_the_variants_come_from_the_definition_not_a_regex(self):
        from fae import experiment as _experiment
        for vid in _experiment.definition().ids:
            self.assertEqual(runs.parse_cell_id(f"m_high_{vid}_T1_r1")[1], vid)
