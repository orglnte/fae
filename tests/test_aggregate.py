"""fae/scoring/aggregate.py — the metrics that reach the paper.

The defect count is the PRIMARY metric (H1a), produced by an LLM judge over
graded cells. What it averages over is therefore a methodological choice, not
an implementation detail, and it had never been pinned by a test.
"""
import importlib.util as _ilu
import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

from _ctx import ROOT

_spec = _ilu.spec_from_file_location("aggregate", Path(ROOT) / "fae" / "scoring" / "aggregate.py")
aggregate = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(aggregate)


def cell(green, defects=None, lines=100, itg=None, e2e=(7, 7)):
    """One row in the shape summarise() consumes."""
    return {
        "green": green,
        "consistency_defect_count": defects,
        "iterations_to_green": itg,
        "e2e_pass": e2e[0], "e2e_total": e2e[1], "e2e_green": green,
        "revoked": False, "budget_exhausted": not green,
        "author_surface": {"files": 3, "language_count": 1, "lines": lines},
    }


class TestDefectsAreGreenOnly(unittest.TestCase):
    """A non-green cell's files are whatever its last failed attempt left —
    possibly mid-edit. Averaging those against delivered implementations
    couples H1a to the success metric, which the hypothesis needs independent.

    Same rule _load_error_rate already applied to load errors; this metric
    simply never got it.
    """

    def test_non_green_cells_are_excluded_from_the_mean(self):
        cells = [cell(True, defects=0), cell(True, defects=0),
                 cell(False, defects=9), cell(False, defects=9)]
        s = aggregate.cell_metrics(cells)
        self.assertEqual(s["mean_consistency_defects"], 0.0,
                         "a non-green cell's defects reached the mean")

    def test_failing_early_cannot_lower_the_mean(self):
        """The specific perverse incentive: a cell that authored almost
        nothing before dying must not make an arm look cleaner."""
        honest = aggregate.cell_metrics([cell(True, defects=2), cell(True, defects=2)])
        gamed = aggregate.cell_metrics([cell(True, defects=2), cell(True, defects=2),
                                     cell(False, defects=0), cell(False, defects=0)])
        self.assertEqual(honest["mean_consistency_defects"],
                         gamed["mean_consistency_defects"])

    def test_n_green_graded_reports_the_real_denominator(self):
        """Reps counts cells; the defect mean is over a SUBSET of them, so the
        denominator has to travel with the number or a mean over one cell
        reads like a rate."""
        cells = [cell(True, defects=1), cell(True, defects=None),
                 cell(False, defects=5)]
        s = aggregate.cell_metrics(cells)
        self.assertEqual(s["n_green_graded"], 1)
        self.assertEqual(s["mean_consistency_defects"], 1.0)

    def test_no_graded_green_cells_yields_None_not_zero(self):
        """0.0 would claim 'no defects found'. None says 'not measured'."""
        s = aggregate.cell_metrics([cell(True, defects=None), cell(False, defects=3)])
        self.assertIsNone(s["mean_consistency_defects"])
        self.assertEqual(s["n_green_graded"], 0)

    def test_ungraded_green_cells_do_not_count_as_zero(self):
        s = aggregate.cell_metrics([cell(True, defects=2), cell(True, defects=None)])
        self.assertEqual(s["mean_consistency_defects"], 2.0)
        self.assertEqual(s["n_green_graded"], 1)


class TestOtherMetricsStillSpanAllCells(unittest.TestCase):
    """Rate metrics span every cell. Narrowing those would silently drop the
    failures the study is measuring."""

    def test_green_rate_counts_every_cell(self):
        s = aggregate.cell_metrics([cell(True), cell(False), cell(False), cell(False)])
        self.assertAlmostEqual(s["green_rate"], 0.25)

    def test_the_all_cells_line_count_survives_as_a_separate_figure(self):
        """The reported LoC is green-only (operator decision 2026-08-03), but
        the all-cells figure answers a different question — what the arm cost
        in authored code, failures included — so it stays in results.json under
        its own name rather than being dropped."""
        s = aggregate.cell_metrics([cell(True, lines=100), cell(False, lines=300)])
        self.assertEqual(s["mean_lines_all_cells"], 200)
        self.assertEqual(s["mean_lines"], 100)

    def test_itg_ignores_cells_that_never_greened(self):
        """iterations_to_green is None for a non-green cell, so mean() skips
        it — censored, not zero."""
        s = aggregate.cell_metrics([cell(True, itg=2), cell(True, itg=4), cell(False)])
        self.assertEqual(s["mean_iterations_to_green"], 3)
        self.assertEqual(s["min_iterations_to_green"], 2)
        self.assertEqual(s["max_iterations_to_green"], 4)


class TestAgentTime(unittest.TestCase):
    """Minutes to green span GREEN cells only, as ITG; minutes per attempt span
    every cell that has them."""

    def _cell(self, green, total, per):
        c = cell(green)
        c.update(agent_s_total=total, agent_s_per_attempt=per)
        return c

    def test_time_to_green_ignores_cells_that_never_greened(self):
        s = aggregate.cell_metrics([self._cell(True, 600, 300), self._cell(True, 1200, 400),
                                    self._cell(False, 6000, 600)])
        self.assertEqual((s["min_agent_s_to_green"], s["mean_agent_s_to_green"],
                          s["max_agent_s_to_green"]), (600, 900, 1200))
        self.assertAlmostEqual(s["mean_agent_s_per_attempt"], 433.333, places=2)

    def test_the_cells_print_minutes_and_a_dash_without_data(self):
        s = aggregate.cell_metrics([self._cell(True, 600, 300), self._cell(True, 1200, 300)])
        self.assertEqual(aggregate.format_agent_time(s), ("10 / 15.0 / 20", "5.0"))
        s = aggregate.cell_metrics([self._cell(True, None, None)])
        self.assertEqual(aggregate.format_agent_time(s), ("-", "-"))


class TestShortModelStripsGatewayPaths(unittest.TestCase):
    def test_provider_path_keeps_only_the_model(self):
        self.assertEqual(aggregate.short_model("opencode-go/deepseek-v4-flash"),
                         "deepseek-v4f")
        self.assertEqual(aggregate.short_model("opencode-go/kimi-k3"), "kimi-k3")

    def test_existing_names_unchanged(self):
        self.assertEqual(aggregate.short_model("claude-sonnet-5"), "sonnet-5")
        self.assertEqual(aggregate.short_model("Gemini 3.1 Pro (High)"),
                         "gemini-3.1p")


class TestRowOrdering(unittest.TestCase):
    """Rows group by model, then rank BEST FIRST: green rate descending, then
    mean iterations-to-green ascending. Alphabetical-by-variant said nothing;
    a reader comparing two variants of one model had to do it by eye."""

    def rows(self, cells):
        return list(aggregate.group_and_rank(cells))

    def test_higher_green_rate_ranks_first(self):
        cells = ([dict(cell(False), model="m", variant="lo_c")] * 3
                 + [dict(cell(True, itg=9), model="m", variant="hi_c")] * 3)
        got = self.rows(cells)
        self.assertTrue(got[0].endswith("hi_c"), got)

    def test_equal_green_rate_breaks_on_faster_itg(self):
        cells = ([dict(cell(True, itg=8), model="m", variant="slow_c")] * 2
                 + [dict(cell(True, itg=1), model="m", variant="fast_c")] * 2)
        got = self.rows(cells)
        self.assertTrue(got[0].endswith("fast_c"), got)

    def test_variants_with_no_green_sort_LAST_not_first(self):
        """mean_itg is None there; a naive None-as-zero would rank them best."""
        cells = ([dict(cell(False), model="m", variant="never_c")] * 3
                 + [dict(cell(True, itg=7), model="m", variant="some_c")] * 3)
        got = self.rows(cells)
        self.assertTrue(got[-1].endswith("never_c"), got)

    def test_pooled_model_ids_share_one_row(self):
        """POOLED_MODELS folds a model id into another at grouping only."""
        cells = ([dict(cell(True, itg=2), model="claude-fable-5", variant="t_c")] * 2
                 + [dict(cell(True, itg=4), model="claude-fable-5-1", variant="t_c")] * 2)
        got = aggregate.group_and_rank(cells)
        self.assertEqual(list(got), ["fable-5.1/5 / t_c"])
        self.assertEqual(got["fable-5.1/5 / t_c"]["n_cells"], 4)
        self.assertEqual(aggregate.short_model("fable-5.1/5"), "fable-5.1/5")

    def test_a_single_id_can_carry_a_custom_row_label(self):
        cells = [dict(cell(True, itg=1), model="Gemini 3.1 Pro (High)", variant="t_c")] * 2
        self.assertEqual(list(aggregate.group_and_rank(cells)), ["g31pro / t_c"])

    def test_models_stay_grouped(self):
        cells = ([dict(cell(True, itg=9), model="aaa", variant="t_c")] * 2
                 + [dict(cell(True, itg=1), model="zzz", variant="t_c")] * 2)
        got = self.rows(cells)
        self.assertTrue(got[0].startswith("aaa"), got)
        self.assertTrue(got[-1].startswith("zzz"), got)


class TestTableColumns(unittest.TestCase):
    """Header and rows must stay in step — a silently misaligned column is a
    number attributed to the wrong metric."""

    def test_header_order_is_itg_then_loc_no_defects(self):
        hdrs = [h for h, _ in aggregate.table_columns(None)]
        self.assertLess(hdrs.index("ITG mn/avg/mx"), hdrs.index("SLoC avg -mn/+mx"))
        self.assertNotIn("DEFECTS", hdrs)

    def test_row_order_matches_the_header(self):
        src = (Path(ROOT) / "fae" / "scoring" / "aggregate.py").read_text()
        row = next(l for l in src.splitlines() if "[str(n), e2e, grn, itg, agent_min, per_att, lines]" in l)
        hdrs = [h for h, _ in aggregate.table_columns(None)]
        self.assertLess(row.index("itg"), row.index("agent_min"))
        self.assertLess(row.index("agent_min"), row.index("per_att"))
        self.assertLess(row.index("per_att"), row.index("lines"))
        self.assertEqual(hdrs[hdrs.index("ITG mn/avg/mx") + 1:hdrs.index("SLoC avg -mn/+mx")],
                         ["MIN mn/avg/mx", "MIN/ATT"])

    def test_a_row_is_a_model_and_a_variant(self):
        self.assertEqual([h for h, _ in aggregate.table_columns(None)][:2], ["MODEL", "VARIANT"])

    def test_the_baseline_comparison_is_two_columns(self):
        hdrs = [h for h, _ in aggregate.table_columns("bash")]
        self.assertEqual(hdrs[-3:], ["N: GRN%  Δpt", "ITG  Δ", "SIG(p)  GRADE"])
        without = [h for h, _ in aggregate.table_columns(None)]
        self.assertNotIn("N: GRN%  Δpt", without)

    def test_auth_lines_label_is_gone(self):
        src = (Path(ROOT) / "fae" / "scoring" / "aggregate.py").read_text()
        self.assertNotIn("AUTH_LINES", src)

    def test_the_loc_column_is_wide_enough_for_its_header_and_values(self):
        """A narrow field does not truncate, it silently stops padding, and the
        DEFECTS column walks left under the wrong header."""
        width = dict(aggregate.table_columns(None))["SLoC avg -mn/+mx"]
        self.assertGreaterEqual(width, aggregate.LOC_MEAN_W + len("   -9999/+9999"))
        self.assertGreaterEqual(width, len("SLoC avg -mn/+mx"))

    def test_the_loc_offsets_start_at_a_fixed_column(self):
        """They are read down the column; a 3-digit mean next to a 4-digit one
        would otherwise put the two rows' offsets out of line."""
        rendered = [aggregate.format_loc(m, lo, hi) for m, lo, hi in
                    ((90, 80, 100), (999, 1, 9999), (1015, 907, 1150))]
        starts = {r.index("-") for r in rendered}
        self.assertEqual(len(starts), 1, rendered)
        self.assertEqual(starts.pop(), aggregate.LOC_MEAN_W + 3)

    def test_an_empty_loc_cell_keeps_the_mean_field_width(self):
        """Otherwise a no-green row's dash sits under the offsets instead of
        under the means."""
        self.assertEqual(len(aggregate.format_loc(None, None, None)),
                         aggregate.LOC_MEAN_W)


class TestLocSpread(unittest.TestCase):
    """LoC gained min/max alongside the mean: a mean alone cannot distinguish
    an arm whose cells all land on one size from one that ranges 3x."""

    def test_min_and_max_span_the_cells(self):
        m = aggregate.cell_metrics([cell(True, lines=n) for n in (120, 300, 240)])
        self.assertEqual(m["min_lines"], 120)
        self.assertEqual(m["max_lines"], 300)
        self.assertEqual(m["mean_lines"], 220)

    def test_non_green_cells_are_excluded(self):
        """Same rule as ITG and DEFECTS, so one row is one population. A
        non-green cell's code does not work; its size is not comparable to a
        delivered solution, and mixing the two would make LoC move with the
        arm's green rate."""
        m = aggregate.cell_metrics([cell(True, lines=500), cell(False, lines=40)])
        self.assertEqual(m["min_lines"], 500)
        self.assertEqual(m["mean_lines"], 500)
        self.assertEqual(m["max_lines"], 500)

    def test_an_arm_with_no_green_reports_no_loc(self):
        m = aggregate.cell_metrics([cell(False, lines=40)] * 3)
        self.assertIsNone(m["mean_lines"])

    def test_files_and_languages_share_the_population(self):
        """They come off the same author_surface; a split population would make
        'mean_files' and 'mean_lines' describe different sets of cells."""
        m = aggregate.cell_metrics([cell(True, lines=500), cell(False, lines=40)])
        self.assertEqual(m["n_green_surface"], 1)
        self.assertEqual(m["mean_files"], 3)

    def test_missing_surface_does_not_read_as_zero(self):
        m = aggregate.cell_metrics([{"green": True, "author_surface": None}])
        self.assertIsNone(m["min_lines"])
        self.assertIsNone(m["max_lines"])


class TestTheSummaryCarriesTheExperimentsEntries(unittest.TestCase):
    """The gaps between arms and the reading notes are the experiment's
    (`Definition.report_summary`); the engine hands it its own metric
    function and delta so the numbers are the table's, and merges whatever
    comes back. The fixture declares none, so nothing is added."""

    def test_the_hook_gets_the_engines_metrics_and_delta(self):
        seen = {}

        class D:
            def report_summary(self, cells, metrics_of, delta, metrics):
                seen.update(cells=cells, metrics_of=metrics_of, delta=delta, metrics=metrics)
                return {"my_gap": {"x": 1}, "notes": "mine"}
        with unittest.mock.patch.object(aggregate, "_definition", return_value=D()):
            out = aggregate.experiment_summary([cell(True)], ("green_rate",))
        self.assertEqual(out, {"my_gap": {"x": 1}, "notes": "mine"})
        self.assertIs(seen["metrics_of"], aggregate.cell_metrics)
        self.assertIs(seen["delta"], aggregate.delta)
        self.assertEqual(seen["metrics"], ["green_rate"])

    def test_a_definition_without_the_hook_adds_nothing(self):
        from fae.cell import experiment as _experiment
        self.assertEqual(aggregate.experiment_summary([cell(True)], ("green_rate",)), {})
        self.assertFalse(hasattr(_experiment.current().module, "report_summary"))

    def test_the_engine_names_no_arm_of_its_experiment(self):
        import re
        from fae.cell import experiment as _experiment
        d = _experiment.current()
        src = (Path(ROOT) / "fae" / "scoring" / "aggregate.py").read_text()
        for word in set(d.ids) | {f for c in d.variants.values() for f in c.FACTORS.values()}:
            self.assertIsNone(re.search(rf"\b{re.escape(word)}\b", src), word)


if __name__ == "__main__":
    unittest.main()


class TestScoreboardCuts(unittest.TestCase):
    """--variant and --impl each cut the corpus and say so; impl is read off
    cell.env for records written before it was part of score.json."""

    def rows(self):
        return [dict(variant="v_apidocs", factors={"docs": "apidocs"}, impl="bash", cell_id="a"),
                dict(variant="v_apidocs", factors={"docs": "apidocs"}, impl="py", cell_id="b"),
                dict(variant="v_howto", factors={"docs": "howto"}, impl="py", cell_id="c")]

    def test_impl_cut(self):
        cells, banners = aggregate.filter_cells(self.rows(), impl="py")
        self.assertEqual([c["cell_id"] for c in cells], ["b", "c"])
        self.assertEqual(banners, ["FILTERED: impl 'py' only — showing 2 of 3 scored cell(s)"])

    def test_variant_then_impl(self):
        cells, banners = aggregate.filter_cells(self.rows(), "v_apidocs", "py")
        self.assertEqual([c["cell_id"] for c in cells], ["b"])
        self.assertEqual(len(banners), 2)
        self.assertIn("1 of 2", banners[1])

    def test_a_factor_level_cut(self):
        cells, banners = aggregate.filter_cells(self.rows(), where={"docs": "howto"})
        self.assertEqual([c["cell_id"] for c in cells], ["c"])
        self.assertEqual(banners, ["FILTERED: factor docs 'howto' only — showing 1 of 3 "
                                   "scored cell(s)"])

    def test_no_cut_no_banner(self):
        cells, banners = aggregate.filter_cells(self.rows())
        self.assertEqual(len(cells), 3)
        self.assertEqual(banners, [])

    def test_each_driver_is_compared_with_its_predecessor(self):
        """bash -> py -> fae: --impl fae is checked against py, --impl py
        against bash, so the whole chain of workspace datasets cross-checks."""
        self.assertEqual(aggregate.PREVIOUS_IMPL, {"fae": "py", "py": "bash"})

    def test_the_engine_records_itself_as_fae(self):
        from fae.cell import IMPL, Cell
        self.assertEqual(IMPL, "fae")
        self.assertEqual(Cell.IMPL, IMPL)

    def test_impl_is_read_off_cell_env_for_old_records(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "cell.env").write_text("MODEL=x\nIMPL=py\n")
            self.assertEqual(aggregate.impl_of(ws), "py")
            (ws / "cell.env").write_text("MODEL=x\n")
            self.assertEqual(aggregate.impl_of(ws), "bash")
            self.assertEqual(aggregate.impl_of(ws / "nope"), "bash")


class TestBaselineCompare(unittest.TestCase):
    """--impl py rows carry the bash row of the same model/arm/variant: its
    n, green rate and ITG, and the py minus bash deltas."""

    def mtc(self, green_rate, itg, n):
        return {"green_rate": green_rate, "mean_iterations_to_green": itg,
                "n_cells": n}

    def test_deltas_are_cut_minus_baseline(self):
        cut = {"a / x/apidocs": self.mtc(0.8, 2.5, 5)}
        base = {"a / x/apidocs": self.mtc(0.6, 3.0, 12)}
        c = aggregate.baseline_compare(cut, base)["a / x/apidocs"]
        self.assertEqual(c["n"], 12)
        self.assertAlmostEqual(c["d_green"], 0.2)
        self.assertAlmostEqual(c["d_itg"], -0.5)
        self.assertEqual(aggregate.format_compare(c), ("12: %60  +20", "3.0  -0.5", "-"))

    def test_a_row_without_a_baseline_compares_to_nothing(self):
        c = aggregate.baseline_compare({"a / x/apidocs": self.mtc(1.0, 1.0, 2)}, {})
        self.assertIsNone(c["a / x/apidocs"])
        self.assertEqual(aggregate.format_compare(None), ("-", "-", "-"))

    def test_no_green_on_either_side_leaves_itg_blank(self):
        cut = {"k": self.mtc(0.0, None, 3)}
        base = {"k": self.mtc(0.0, None, 10)}
        c = aggregate.baseline_compare(cut, base)["k"]
        self.assertIsNone(c["d_itg"])
        self.assertEqual(aggregate.format_compare(c), ("10: %0  +0", "-", "-"))


class TestDiscrepancy(unittest.TestCase):
    """--sort-discrepancy ranks rows by how far the cut moved from its
    baseline; discrepancy() is the one number that ranking is built on."""

    def mtc(self, green_rate, itg, n):
        return {"green_rate": green_rate, "mean_iterations_to_green": itg,
                "n_cells": n}

    def test_no_baseline_has_no_discrepancy(self):
        self.assertIsNone(aggregate.discrepancy(None))

    def test_discrepancy_is_the_absolute_green_rate_delta(self):
        cut = {"k": self.mtc(0.3, 2.0, 5)}
        base = {"k": self.mtc(0.8, 2.0, 5)}
        c = aggregate.baseline_compare(cut, base)["k"]
        self.assertAlmostEqual(aggregate.discrepancy(c), 0.5)

    def test_falls_back_to_the_itg_delta_when_green_rate_is_incomparable(self):
        # green_rate itself missing on both sides -> d_green is None; ITG
        # is present on both -> d_itg is a real number to fall back to.
        cut = {"k": self.mtc(None, 2.0, 3)}
        base = {"k": self.mtc(None, 7.0, 10)}
        c = aggregate.baseline_compare(cut, base)["k"]
        self.assertIsNone(c["d_green"])
        self.assertAlmostEqual(aggregate.discrepancy(c), 5.0)

    def test_neither_metric_comparable_is_no_discrepancy(self):
        # green_rate itself is None (not just ITG) on both sides: nothing
        # for delta() to subtract, so both deltas come back None.
        cut = {"k": self.mtc(None, None, 0)}
        base = {"k": self.mtc(None, None, 5)}
        c = aggregate.baseline_compare(cut, base)["k"]
        self.assertIsNone(c["d_green"])
        self.assertIsNone(c["d_itg"])
        self.assertIsNone(aggregate.discrepancy(c))


class TestSortDiscrepancy(unittest.TestCase):
    """order_rows backs --sort-discrepancy: without it, group_and_rank's own
    order stands; with it, the row that moved furthest from its baseline
    prints first, and a row with no baseline at all sorts last rather than
    reading as 'no gap' at the top."""

    def mtc(self, green_rate, itg, n):
        return {"green_rate": green_rate, "mean_iterations_to_green": itg,
                "n_cells": n}

    def test_default_order_is_unchanged(self):
        by_mtc = {"a": {}, "b": {}, "c": {}}
        self.assertEqual(aggregate.order_rows(by_mtc, None, sort_discrepancy=False),
                         list(by_mtc.items()))
        self.assertEqual(aggregate.order_rows(by_mtc, {}, sort_discrepancy=False),
                         list(by_mtc.items()))

    def test_sort_discrepancy_without_a_baseline_is_a_no_op(self):
        by_mtc = {"a": {}, "b": {}}
        self.assertEqual(aggregate.order_rows(by_mtc, None, sort_discrepancy=True),
                         list(by_mtc.items()))

    def test_biggest_gap_first_no_baseline_last(self):
        by_mtc = {"small": {}, "big": {}, "none": {}}
        compare = {
            "small": aggregate.baseline_compare(
                {"small": self.mtc(0.7, 2.0, 5)}, {"small": self.mtc(0.6, 2.0, 5)})["small"],
            "big": aggregate.baseline_compare(
                {"big": self.mtc(0.1, 2.0, 5)}, {"big": self.mtc(0.9, 2.0, 5)})["big"],
            "none": None,
        }
        got = [k for k, _ in aggregate.order_rows(by_mtc, compare, sort_discrepancy=True)]
        self.assertEqual(got, ["big", "small", "none"])


class TestFisherExactP(unittest.TestCase):
    """fisher_exact_p is the significance test behind SIG(p): exact
    hypergeometric summation, no scipy, so it must be pinned against known
    values rather than trusted by construction."""

    def test_identical_tiny_samples_are_not_significant(self):
        # 1/2 vs 1/2: the perfectly balanced table under its own margins is
        # the observed one, so nothing is at least as extreme but itself —
        # the two-sided p is exactly 1.0, not merely "high".
        self.assertAlmostEqual(aggregate.fisher_exact_p(1, 1, 1, 1), 1.0)

    def test_a_stark_split_is_exactly_computable(self):
        # 10/10 green vs 0/10 green: the two most extreme tables under
        # fixed margins (10, 10, 10, 10) are k=10 and its mirror k=0; the
        # exact two-sided p is 2 * (10 choose 10)(10 choose 0)/(20 choose 10).
        from math import comb
        expected = 2 * comb(10, 10) * comb(10, 0) / comb(20, 10)
        self.assertAlmostEqual(aggregate.fisher_exact_p(10, 0, 0, 10), expected)
        self.assertLess(aggregate.fisher_exact_p(10, 0, 0, 10), 0.001)

    def test_symmetric_in_which_side_is_cut_vs_baseline(self):
        # Swapping the rows must not change the p-value: "how surprising is
        # this split" does not depend on which side we call the baseline.
        self.assertAlmostEqual(aggregate.fisher_exact_p(3, 2, 1, 4),
                               aggregate.fisher_exact_p(1, 4, 3, 2))

    def test_a_zero_margin_has_no_test(self):
        # Every one of these makes one row or column empty: nothing to
        # compare against, and the exact test's own combinatorics would
        # divide by comb(total, 0) or similar degenerate cases.
        self.assertIsNone(aggregate.fisher_exact_p(0, 0, 3, 4))   # cut ran 0 cells
        self.assertIsNone(aggregate.fisher_exact_p(3, 4, 0, 0))   # baseline ran 0 cells
        self.assertIsNone(aggregate.fisher_exact_p(0, 5, 0, 7))   # nobody ever greened
        self.assertIsNone(aggregate.fisher_exact_p(5, 0, 7, 0))   # everybody always greened

    def test_p_never_exceeds_one(self):
        for a, b, c, d in [(1, 1, 1, 1), (2, 2, 2, 2), (5, 5, 5, 5)]:
            p = aggregate.fisher_exact_p(a, b, c, d)
            self.assertIsNotNone(p)
            self.assertLessEqual(p, 1.0)
            self.assertGreaterEqual(p, 0.0)


class TestFormatSignificance(unittest.TestCase):

    def test_no_p_value_is_a_dash(self):
        self.assertEqual(aggregate.format_significance(None), "-")

    def test_below_01_gets_two_stars_and_the_lt_form(self):
        self.assertEqual(aggregate.format_significance(0.001), "p<.01**")

    def test_below_05_gets_one_star(self):
        self.assertEqual(aggregate.format_significance(0.03), "p0.03*")

    def test_at_or_above_05_gets_no_star(self):
        self.assertEqual(aggregate.format_significance(0.41), "p0.41")

    def test_exactly_at_a_threshold_is_not_significant(self):
        # Strict inequality: p == .05 is conventionally NOT significant.
        self.assertEqual(aggregate.format_significance(0.05), "p0.05")
        self.assertEqual(aggregate.format_significance(0.01), "p0.01*")


class TestSignificanceGrade(unittest.TestCase):
    """significance_grade is the plain-English read of the same p-value
    format_significance already prints as a number: a bare 'p1.00' is easy
    to misread as '100% sure' rather than 'no evidence of a difference'."""

    def test_no_p_value_is_a_dash(self):
        self.assertEqual(aggregate.significance_grade(None), "-")

    def test_below_01_is_strong(self):
        self.assertEqual(aggregate.significance_grade(0.001), "strong")

    def test_below_05_is_significant(self):
        self.assertEqual(aggregate.significance_grade(0.03), "significant")

    def test_at_or_above_05_is_not_sig(self):
        self.assertEqual(aggregate.significance_grade(0.41), "not sig")
        self.assertEqual(aggregate.significance_grade(1.0), "not sig")

    def test_thresholds_match_format_significance_exactly(self):
        # The two must never disagree on WHICH side of a threshold a given
        # p falls — a row where the number says one thing and the word
        # says another would be worse than not having the word at all.
        for p in (0.001, 0.009999, 0.01, 0.0100001, 0.03, 0.049999, 0.05, 0.5, 1.0):
            marker = "**" if p < 0.01 else "*" if p < 0.05 else ""
            grade = aggregate.significance_grade(p)
            if marker == "**":
                self.assertEqual(grade, "strong", p)
            elif marker == "*":
                self.assertEqual(grade, "significant", p)
            else:
                self.assertEqual(grade, "not sig", p)


class TestBaselineCompareCarriesFisherP(unittest.TestCase):
    """baseline_compare computes fisher_p from the raw green counts, not the
    rates — so the fixture here has to set n_green explicitly, unlike the
    other baseline_compare tests, which never populate it (and so always
    land on the degenerate zero-margin case)."""

    def mtc(self, green_rate, itg, n, n_green):
        return {"green_rate": green_rate, "mean_iterations_to_green": itg,
                "n_cells": n, "n_green": n_green}

    def test_a_real_split_produces_a_real_p_value(self):
        cut = {"k": self.mtc(0.2, 2.0, 10, 2)}
        base = {"k": self.mtc(0.9, 2.0, 10, 9)}
        c = aggregate.baseline_compare(cut, base)["k"]
        self.assertIsNotNone(c["fisher_p"])
        self.assertLess(c["fisher_p"], 0.05)
        self.assertTrue(aggregate.format_significance(c["fisher_p"]).endswith("*"))

    def test_format_compare_carries_the_significance_field(self):
        cut = {"k": self.mtc(0.2, 2.0, 10, 2)}
        base = {"k": self.mtc(0.9, 2.0, 10, 9)}
        c = aggregate.baseline_compare(cut, base)["k"]
        grn, itg, sig = aggregate.format_compare(c)
        expected = (f"{aggregate.format_significance(c['fisher_p'])}  "
                   f"{aggregate.significance_grade(c['fisher_p'])}")
        self.assertEqual(sig, expected)
        self.assertIn("*", sig)
        self.assertIn("strong", sig)

    def test_no_p_value_gives_a_single_dash_not_a_padded_one(self):
        c = aggregate.baseline_compare(
            {"k": self.mtc(None, None, 0, 0)}, {"k": self.mtc(None, None, 5, 0)})["k"]
        self.assertIsNone(c["fisher_p"])
        self.assertEqual(aggregate.format_compare(c)[2], "-")


class TestSortSignificant(unittest.TestCase):
    """--sort-significant ranks by Fisher's p; combined with
    --sort-discrepancy, significance gates the ranking and discrepancy
    breaks ties within it — 'high impact' rows (significant AND big) end
    up strictly on top."""

    def mtc(self, green_rate, itg, n, n_green):
        return {"green_rate": green_rate, "mean_iterations_to_green": itg,
                "n_cells": n, "n_green": n_green}

    def compare_for(self, cut_green, cut_n, base_green, base_n, itg=2.0):
        cut = {"k": self.mtc(cut_green / cut_n, itg, cut_n, cut_green)}
        base = {"k": self.mtc(base_green / base_n, itg, base_n, base_green)}
        return aggregate.baseline_compare(cut, base)["k"]

    def test_sort_significant_alone_puts_significant_rows_first(self):
        by_mtc = {"sig": {}, "not_sig": {}, "none": {}}
        compare = {
            "sig": self.compare_for(2, 10, 9, 10),      # stark split -> p<.05
            "not_sig": self.compare_for(5, 10, 6, 10),  # close split -> p>.05
            "none": None,
        }
        got = [k for k, _ in aggregate.order_rows(by_mtc, compare, sort_significant=True)]
        self.assertEqual(got, ["sig", "not_sig", "none"])

    def test_sort_significant_without_a_baseline_is_a_no_op(self):
        by_mtc = {"a": {}, "b": {}}
        self.assertEqual(aggregate.order_rows(by_mtc, None, sort_significant=True),
                         list(by_mtc.items()))

    def test_combined_puts_significant_and_big_strictly_on_top(self):
        by_mtc = {"sig_small": {}, "not_sig_big": {}, "sig_big": {}, "none": {}}
        compare = {
            # significant, small green-rate gap (delta .15 on n=100 each side
            # is a comfortable margin below alpha, not a knife-edge one)
            "sig_small": self.compare_for(95, 100, 80, 100),
            # NOT significant, huge gap on a tiny n (too few cells to be sure)
            "not_sig_big": self.compare_for(1, 1, 0, 1),
            # significant AND a big gap
            "sig_big": self.compare_for(9, 10, 0, 10),
            "none": None,
        }
        got = [k for k, _ in aggregate.order_rows(
            by_mtc, compare, sort_discrepancy=True, sort_significant=True)]
        # significant rows (sig_big, sig_small) both outrank the
        # not-significant one regardless of its raw discrepancy, and
        # within the significant group the bigger gap (sig_big) leads.
        self.assertEqual(got, ["sig_big", "sig_small", "not_sig_big", "none"])


class TestTaintedCellsAreExcludedByDefault(unittest.TestCase):
    """A cell the validator distrusts must not move a mean silently: excluded
    unless asked, and named either way."""

    def corpus(self, d):
        import json
        for cid, verdict in (("a_high_beta_apidocs_T1_r1", "VALID"),
                             ("b_high_beta_apidocs_T1_r1", "TAINTED"),
                             ("c_high_beta_apidocs_T1_r1", None)):
            ws = Path(d) / cid
            ws.mkdir()
            (ws / "score.json").write_text(json.dumps({"cell_id": cid, "impl": "py", "green": True}))
            if verdict:
                (ws / "validation.json").write_text(json.dumps(
                    {"verdict": verdict, "taints": ["rule 12: x"] if verdict == "TAINTED" else []}))

    def test_default_drops_tainted_and_reports_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.corpus(d)
            with unittest.mock.patch.object(aggregate, "WORKSPACES", Path(d)):
                cells, excluded = aggregate.load_cells()
        self.assertEqual(sorted(c["cell_id"][0] for c in cells), ["a", "c"])
        self.assertEqual(excluded, [("b_high_beta_apidocs_T1_r1", "rule 12: x")])

    def test_include_tainted_keeps_them(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            self.corpus(d)
            with unittest.mock.patch.object(aggregate, "WORKSPACES", Path(d)):
                cells, excluded = aggregate.load_cells(include_tainted=True)
        self.assertEqual(len(cells), 3)
        self.assertEqual(excluded, [])


class TestTheTaintWarningNamesCellsOnlyOnRequest(unittest.TestCase):
    """The default scoreboard states it as a filter, like variant/impl; the
    per-cell taints print under --tainted-cells-details."""

    EXCLUDED = [("b_high_beta_apidocs_T1_r1", "rule 12: x"),
                ("d_high_beta_apidocs_T1_r2", "rule 13: y")]

    def test_the_default_line_is_a_filtered_banner_not_the_cells(self):
        lines = aggregate.taint_report(self.EXCLUDED, 8, details=False)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("FILTERED:"))
        self.assertIn("showing 8 of 10", lines[0])
        self.assertIn("--include-tainted", lines[0])
        self.assertNotIn("rule 12", lines[0])

    def test_details_list_every_cell_with_its_taint(self):
        lines = aggregate.taint_report(self.EXCLUDED, 8, details=True)
        self.assertEqual(len(lines), 3)
        self.assertIn("b_high_beta_apidocs_T1_r1: rule 12: x", lines[1])
        self.assertIn("d_high_beta_apidocs_T1_r2: rule 13: y", lines[2])

    def test_nothing_tainted_prints_nothing(self):
        self.assertEqual(aggregate.taint_report([], 8, details=False), [])
        self.assertEqual(aggregate.taint_report([], 8, details=True), [])

    def test_the_details_flag_prints_only_the_list(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            TestTaintedCellsAreExcludedByDefault().corpus(d)
            out = io.StringIO()
            with unittest.mock.patch.object(aggregate, "WORKSPACES", Path(d)), \
                    unittest.mock.patch.object(sys, "argv", ["aggregate.py", "--tainted-cells-details"]), \
                    redirect_stdout(out):
                rc = aggregate.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().splitlines()[-1],
                         "  b_high_beta_apidocs_T1_r1: rule 12: x")
        self.assertNotIn("Scoreboard", out.getvalue())
        self.assertFalse((Path(d) / "results.csv").exists())


class TestTheScoreboardRunsEndToEnd(unittest.TestCase):
    """main() over real score.json files, the way `results score` runs it —
    a name left behind by a rename stopped the table before any row printed."""

    def cell(self, root, cid, green):
        d = root / cid
        d.mkdir()
        (d / "score.json").write_text(json.dumps({
            "cell_id": cid, "model": "m-1", "task": "T1", "variant": "beta_apidocs", "factors": {"docs": "apidocs"}, "impl": "py", "repeat": 1,
            "attempt_budget": 10, "consistency_defect_count": None, "codes": [],
            "deploy_ok": True, "e2e_pass": 7, "e2e_total": 7, "e2e_green": green,
            "load_errors": 0, "load_total": 100, "load_ran": True, "k6_available": True,
            "verify_stage_failed": None, "iterations_to_green": 2 if green else None,
            "green": green, "revoked": False, "budget_exhausted": not green,
            "author_surface": {"files": 1, "languages": ["Python"], "language_count": 1,
                               "lines": 10, "sloc": 8}}))

    def test_the_table_prints_for_the_whole_corpus_and_for_one_variant(self):
        import io, tempfile
        from contextlib import redirect_stdout
        root = Path(tempfile.mkdtemp())
        self.cell(root, "m_high_beta_apidocs_T1_r1", True)
        self.cell(root, "m_high_beta_apidocs_T1_r2", False)
        for argv in (["aggregate"], ["aggregate", "--variant", "beta_apidocs"],
                     ["aggregate", "--where", "docs=apidocs"]):
            out = io.StringIO()
            with unittest.mock.patch.object(aggregate, "WORKSPACES", root), \
                 unittest.mock.patch.object(aggregate, "OUT_CSV", root / "results.csv"), \
                 unittest.mock.patch.object(aggregate, "OUT_JSON", root / "results.json"), \
                 unittest.mock.patch.object(sys, "argv", argv), redirect_stdout(out):
                rc = aggregate.main()
            self.assertIn(rc, (0, None), argv)
            self.assertIn("beta", out.getvalue(), argv)
