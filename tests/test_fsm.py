"""fae/cell/fsm.py: the per-cell state machine, driven through real
transitions.

Every test asserts what a cell is AFTER a transition — which events it now
admits, which it refuses, what it holds and what it has spent — so the table
is pinned by what it does, not by what it says.
"""
import unittest

from _ctx import ROOT  # noqa: F401  (puts the repo root on sys.path)
from fae.cell import fsm
from fae.cell.fsm import (IllegalTransition, Loop, Phase, State, T,
                              step)


def run(*ts, s=None):
    s = s or State()
    for t in ts:
        step(s, t)
    return s


def enabled(s):
    return {t for t in T if fsm.ENABLED[t](s)}


def refuses(tc, s, t):
    with tc.assertRaises(IllegalTransition):
        step(s, t)


class TestAFreshCell(unittest.TestCase):

    def test_holds_nothing_and_has_spent_nothing(self):
        s = State()
        self.assertIs(s.loop, Loop.NONE)
        self.assertEqual(s.intent, "run")
        self.assertEqual(s.attempts, 0)
        self.assertIs(s.slot_held, False)
        self.assertIs(s.verify_held, False)
        self.assertIs(s.rig_held, False)
        self.assertIsNone(s.outcome)

    def test_admits_only_spawn_pause_and_kill(self):
        self.assertEqual(enabled(State()), {T.SPAWN, T.PAUSE, T.KILL})

    def test_renders_each_hold_as_a_definite_boolean(self):
        # The repr is what an IllegalTransition shows the operator; a hold
        # rendered as None reads as "unknown" for a cell that holds nothing.
        self.assertEqual(
            repr(State()),
            "State(loop=none intent=run attempts=0 "
            "slot=False verify=False rig=False)")


class TestEveryPhaseHasALoop(unittest.TestCase):

    def test_the_mapping_is_total(self):
        self.assertEqual(set(fsm.PHASE_TO_LOOP), set(Phase))
        self.assertTrue(all(isinstance(l, Loop)
                            for l in fsm.PHASE_TO_LOOP.values()))


class TestAcquiring(unittest.TestCase):

    def test_the_slot_is_held_and_the_attempt_is_charged(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT)
        self.assertIs(s.slot_held, True)
        self.assertIs(s.loop, Loop.AGENT)
        self.assertEqual(s.attempts, 1)
        self.assertNotIn(T.ACQUIRE_SLOT, enabled(s))

    def test_the_verify_is_held_while_verifying(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY)
        self.assertIs(s.verify_held, True)
        self.assertIs(s.loop, Loop.VERIFY)
        self.assertEqual(
            enabled(s),
            {T.ACQUIRE_RIG, T.VERIFY_GREEN, T.VERIFY_FAIL, T.RELEASE_SLOT,
             T.CRASH, T.PAUSE, T.KILL})

    def test_the_rig_is_taken_released_and_can_be_taken_again(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.ACQUIRE_RIG)
        self.assertIs(s.rig_held, True)
        refuses(self, s, T.ACQUIRE_RIG)
        step(s, T.RELEASE_RIG)
        self.assertIs(s.rig_held, False)
        refuses(self, s, T.RELEASE_RIG)
        step(s, T.ACQUIRE_RIG)
        self.assertIs(s.rig_held, True)


class TestAGreenVerdict(unittest.TestCase):

    def test_ends_the_loop_and_gives_everything_back(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.ACQUIRE_RIG,
                T.VERIFY_GREEN)
        self.assertEqual(s.outcome, "green")
        self.assertIs(s.loop, Loop.NONE)
        self.assertIs(s.slot_held, False)
        self.assertIs(s.verify_held, False)
        self.assertIs(s.rig_held, False)
        self.assertEqual(s.attempts, 1)

    def test_a_finished_cell_holds_nothing_to_release_and_cannot_respawn(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.ACQUIRE_RIG,
                T.VERIFY_GREEN)
        self.assertEqual(enabled(s), {T.PAUSE, T.KILL})
        refuses(self, s, T.RELEASE_RIG)
        refuses(self, s, T.SPAWN)


class TestAFailedVerdict(unittest.TestCase):

    def test_returns_the_cell_to_the_agent_keeping_the_slot_and_the_charge(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.VERIFY_FAIL)
        self.assertIs(s.loop, Loop.AGENT)
        self.assertIs(s.slot_held, True)
        self.assertIs(s.verify_held, False)
        self.assertEqual(s.attempts, 1)
        self.assertIsNone(s.outcome)
        self.assertIn(T.ACQUIRE_VERIFY, enabled(s))


class TestAbandoningAnAttempt(unittest.TestCase):
    """RELEASE_SLOT and CRASH end the loop from anywhere; the cell must then
    be respawnable, and an attempt abandoned before judgment is refunded."""

    def test_ending_the_loop_frees_every_hold_and_lets_the_cell_respawn(self):
        for t in (T.RELEASE_SLOT, T.CRASH):
            with self.subTest(t=t.value):
                s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY,
                        T.ACQUIRE_RIG, t)
                self.assertIs(s.loop, Loop.NONE)
                self.assertIs(s.slot_held, False)
                self.assertIs(s.verify_held, False)
                self.assertIs(s.rig_held, False)
                refuses(self, s, T.RELEASE_RIG)
                step(s, T.SPAWN)
                step(s, T.ACQUIRE_SLOT)
                self.assertIs(s.loop, Loop.AGENT)

    def test_an_unjudged_attempt_is_refunded_a_judged_one_is_not(self):
        for t in (T.RELEASE_SLOT, T.CRASH):
            with self.subTest(t=t.value, where="idle"):
                self.assertEqual(run(T.SPAWN, t).attempts, 0)
            with self.subTest(t=t.value, where="agent"):
                self.assertEqual(run(T.SPAWN, T.ACQUIRE_SLOT, t).attempts, 0)
            with self.subTest(t=t.value, where="verify"):
                self.assertEqual(
                    run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, t).attempts,
                    0)
            with self.subTest(t=t.value, where="after a fail"):
                # The fail was judged and charged; the crash after it refunds
                # only the fresh, unjudged attempt the cell was on.
                s = run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY,
                        T.VERIFY_FAIL, T.ACQUIRE_VERIFY, t)
                self.assertEqual(s.attempts, 0)

    def test_nothing_ends_a_loop_that_is_not_running(self):
        for t in (T.RELEASE_SLOT, T.CRASH):
            with self.subTest(t=t.value):
                refuses(self, State(), t)


class TestStandingDown(unittest.TestCase):
    """The cooperative exit: a paused or killed cell gives its slot back and
    its unjudged attempt is refunded, so the resume redoes it."""

    def test_only_a_non_running_cell_stands_down(self):
        refuses(self, run(T.SPAWN, T.ACQUIRE_SLOT), T.STAND_DOWN)
        refuses(self, run(T.SPAWN, T.ACQUIRE_SLOT, T.ACQUIRE_VERIFY, T.PAUSE),
                T.STAND_DOWN)

    def test_a_paused_cell_stands_down_and_resumes_into_a_fresh_attempt(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.PAUSE, T.STAND_DOWN)
        self.assertIs(s.loop, Loop.NONE)
        self.assertIs(s.slot_held, False)
        self.assertEqual(s.attempts, 0)
        self.assertEqual(s.intent, "paused")
        refuses(self, s, T.SPAWN)
        step(s, T.RESUME)
        step(s, T.SPAWN)
        step(s, T.ACQUIRE_SLOT)
        self.assertIs(s.slot_held, True)
        self.assertEqual(s.attempts, 1)

    def test_standing_down_from_idle_refunds_nothing(self):
        s = run(T.SPAWN, T.PAUSE, T.STAND_DOWN)
        self.assertEqual(s.attempts, 0)
        self.assertIs(s.loop, Loop.NONE)


class TestIntent(unittest.TestCase):

    def test_pause_then_resume_restores_a_running_cell(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.PAUSE)
        self.assertEqual(s.intent, "paused")
        refuses(self, s, T.PAUSE)
        refuses(self, s, T.ACQUIRE_VERIFY)
        step(s, T.RESUME)
        self.assertEqual(s.intent, "run")
        refuses(self, s, T.RESUME)
        refuses(self, s, T.STAND_DOWN)
        step(s, T.ACQUIRE_VERIFY)
        self.assertIs(s.loop, Loop.VERIFY)

    def test_a_resumed_cell_is_admitted_exactly_as_a_fresh_one(self):
        s = run(T.PAUSE, T.RESUME)
        self.assertEqual(enabled(s), enabled(State()))

    def test_kill_is_terminal_for_intent(self):
        for before in ((), (T.PAUSE,), (T.SPAWN, T.ACQUIRE_SLOT)):
            with self.subTest(before=[t.value for t in before]):
                s = run(*before, T.KILL)
                self.assertEqual(s.intent, "killed")
                refuses(self, s, T.KILL)
                refuses(self, s, T.RESUME)
                refuses(self, s, T.PAUSE)
                refuses(self, s, T.SPAWN)

    def test_a_killed_cell_stands_down_and_stays_dead(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT, T.KILL, T.STAND_DOWN)
        self.assertIs(s.loop, Loop.NONE)
        self.assertEqual(s.intent, "killed")
        self.assertEqual(enabled(s), set())


class TestARefusal(unittest.TestCase):

    def test_names_the_transition_and_shows_the_state(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT)
        with self.assertRaises(IllegalTransition) as cm:
            step(s, T.SPAWN)
        msg = str(cm.exception)
        self.assertIn("Spawn is not enabled", msg)
        self.assertIn(repr(s), msg)

    def test_changes_nothing(self):
        s = run(T.SPAWN, T.ACQUIRE_SLOT)
        before = repr(s)
        refuses(self, s, T.ACQUIRE_SLOT)
        self.assertEqual(repr(s), before)

    def test_accepts_the_spec_name_as_a_string(self):
        s = step(State(), "Spawn")
        self.assertIs(s.loop, Loop.IDLE)
        with self.assertRaises(ValueError):
            step(s, "NotATransition")


if __name__ == "__main__":
    unittest.main()
