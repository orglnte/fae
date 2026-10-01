"""Small pure helpers: _tail_hist, never_started, parse_reset."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from _ctx import runs


class TestTailHist(unittest.TestCase):

    def test_short_history_is_untouched(self):
        self.assertEqual(runs.render._tail_hist("scaling ×3", 40), "scaling ×3")

    def test_exact_length_is_untouched(self):
        self.assertEqual(runs.render._tail_hist("abcde", 5), "abcde")

    def test_long_history_is_elided_at_a_token_boundary(self):
        out = runs.render._tail_hist("build,e2e,scaling,scaling,green", 20)
        self.assertTrue(out.startswith("…"))
        self.assertLessEqual(len(out), 20)
        self.assertFalse(out.startswith("…,"), "cut left a leading comma")

    def test_result_is_a_suffix_of_the_input(self):
        h = "aaa,bbb,ccc,ddd,eee,fff"
        out = runs.render._tail_hist(h, 12)
        self.assertTrue(h.endswith(out.lstrip("…")))


class TestNeverStarted(unittest.TestCase):
    """A prepared workspace with zero ledger events is not part of the run.
    reconcile must not 'respawn' it — auto-starting agent runs nobody launched
    is audit finding 13."""

    def test_zero_events_and_crashed_is_never_started(self):
        self.assertTrue(runs.state.never_started({"events": 0, "state": "CRASHED"}))

    def test_events_present_means_it_started(self):
        """A cell whose attempt 1 logged START but never finished also has an
        empty ITER history — counting history instead of events stranded six
        paused-then-crashed cells as 'never started' (2026-07-25)."""
        self.assertFalse(runs.state.never_started({"events": 3, "state": "CRASHED"}))

    def test_non_crashed_states_are_never_never_started(self):
        for state in ("RUNNING", "DONE", "PAUSED"):
            with self.subTest(state=state):
                self.assertFalse(runs.state.never_started({"events": 0, "state": state}))


class TestParseReset(unittest.TestCase):
    """Parses when a rate limit lifts, off the provider's own wording."""

    def test_no_match_is_none(self):
        self.assertIsNone(runs.render.parse_reset("something unrelated"))
        self.assertIsNone(runs.render.parse_reset(""))

    def test_hours_and_minutes(self):
        got = runs.render.parse_reset("Resets in 1h47m")
        self.assertAlmostEqual(got, runs.time.time() + 3600 + 47 * 60, delta=5)

    def test_minutes_only(self):
        got = runs.render.parse_reset("Resets in 30m")
        self.assertAlmostEqual(got, runs.time.time() + 1800, delta=5)

    def test_hours_only(self):
        got = runs.render.parse_reset("Resets in 2h")
        self.assertAlmostEqual(got, runs.time.time() + 7200, delta=5)

    def test_bare_resets_in_without_a_duration_is_none(self):
        self.assertIsNone(runs.render.parse_reset("Resets in a while"))

    def _at(self, when):
        """Freeze runs.render.datetime.now() at `when` (tz-aware UTC)."""
        fake = mock.MagicMock()
        fake.now.return_value = when
        return mock.patch.object(runs.render, "datetime", fake)

    def test_clock_form_later_today(self):
        now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
        with self._at(now):
            got = runs.render.parse_reset("resets 3:10am (UTC)")
        self.assertEqual(datetime.fromtimestamp(got, timezone.utc),
                         datetime(2026, 7, 30, 3, 10, tzinfo=timezone.utc))

    def test_clock_form_pm_is_afternoon(self):
        now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
        with self._at(now):
            got = runs.render.parse_reset("resets 3:10pm (UTC)")
        self.assertEqual(datetime.fromtimestamp(got, timezone.utc).hour, 15)

    def test_clock_form_noon_and_midnight(self):
        now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
        for text, hour in (("resets 12:00pm (UTC)", 12), ("resets 11:00pm (UTC)", 23)):
            with self.subTest(text=text), self._at(now):
                got = runs.render.parse_reset(text)
                self.assertEqual(
                    datetime.fromtimestamp(got, timezone.utc).hour, hour)

    def test_clock_form_rolls_over_month_end(self):
        """Regression: this used to be `t.replace(day=now.day + 1)`.

        Advancing to tomorrow by incrementing the day NUMBER asks for day 32
        on the last day of a month, and datetime raises
        `ValueError: day is out of range for month`. That escaped
        parse_reset into monitor()'s loop, which catches only
        KeyboardInterrupt — so the supervisor died on 31 Jan / 28 Feb /
        31 Mar / … whenever a limit message used the clock form and the
        stated time had already passed. `timedelta(days=1)` carries month
        and year.
        """
        now = datetime(2026, 1, 31, 23, 0, tzinfo=timezone.utc)
        with self._at(now):
            got = runs.render.parse_reset("resets 1:00am (UTC)")
        self.assertEqual(datetime.fromtimestamp(got, timezone.utc),
                         datetime(2026, 2, 1, 1, 0, tzinfo=timezone.utc))

    def test_clock_form_same_day_rollover_within_a_month(self):
        """The non-month-end path, which does work today."""
        now = datetime(2026, 7, 15, 23, 0, tzinfo=timezone.utc)
        with self._at(now):
            got = runs.render.parse_reset("resets 1:00am (UTC)")
        self.assertEqual(datetime.fromtimestamp(got, timezone.utc),
                         datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc))

    def test_duration_form_wins_over_clock_form(self):
        """Both regexes can match one message; the duration branch runs first
        and is the more precise of the two."""
        got = runs.render.parse_reset("Resets in 5m — resets 3:10am (UTC)")
        self.assertAlmostEqual(got, runs.time.time() + 300, delta=5)


class TestConstantsAgree(unittest.TestCase):

    def test_auth_hints_are_a_subset_of_limit_hints(self):
        """Regression: "authentication" was in AUTH_HINTS but not LIMIT_HINTS.

        monitor() gates first on LIMIT_HINTS (`if not any(h in reason for h in
        LIMIT_HINTS): continue`) and only then classifies the survivors as
        AUTH vs limit. A wall matching only "authentication" never passed the
        gate, so it was never surfaced and its AUTH classification was
        unreachable — inverting the stated priority, since limits lift on
        their own and auth walls never do.

        Keep this invariant: anything added to AUTH_HINTS must also be in
        LIMIT_HINTS, or it silently stops being reportable.
        """
        self.assertTrue(set(runs.AUTH_HINTS) <= set(runs.LIMIT_HINTS))

    def test_hints_are_lowercase(self):
        """They are matched against lowercased log text."""
        for h in runs.LIMIT_HINTS + runs.AUTH_HINTS:
            with self.subTest(hint=h):
                self.assertEqual(h, h.lower())


if __name__ == "__main__":
    unittest.main()
