"""The attempt budget is a constant, not a knob.

Attempts-to-green is the study's dependent variable: two cells run at
different budgets are not comparable. A per-cell budget was also the one
documented reason to reopen a finished cell ("censored at a smaller budget
continues from attempt N+1"), which is a standing exception to sealing.

So the test is not "the default is 10" — it is that NO path can produce
anything else: not a flag, not a queue spec, not an environment variable.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from _ctx import ROOT, runs

HARNESS = Path(ROOT) / "fae"
CLI_PY = (Path(ROOT) / "fae" / "cli.py").read_text()
# where a cell is spawned, respawned or queued: the CLI's verbs, the run's
# respawn, the Queues and the Cell's own start
OPS_PY = "".join((Path(ROOT) / "fae" / f).read_text() for f in (
    "cli.py", "driver/conduct/__init__.py", "queues.py", "cell/cell.py"))
CELL_PY = (HARNESS / "cell" / "cell.py").read_text()
PREPARE = (HARNESS / "cell" / "prepare.py").read_text()


class TestTheConstant(unittest.TestCase):

    def test_one_constant(self):
        from fae.cell import ATTEMPT_BUDGET
        self.assertEqual(ATTEMPT_BUDGET, 10)
        self.assertIn("ATTEMPT_BUDGET = 10", CELL_PY)

    def test_the_constant_wins_over_the_environment(self):
        # The behavioural claim: an inherited ATTEMPT_BUDGET cannot change it.
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        v = cfgmod.load(ROOT, env=dict(os.environ, ATTEMPT_BUDGET="3"))
        self.assertEqual(v.get("ATTEMPT_BUDGET"), "10")


class TestNoPathCanSetADifferentBudget(unittest.TestCase):
    """spawn/spawn-matrix/top-up are cli.py's verbs over the Queues, the
    Conduct and the Cell — a budget could be reintroduced at any of them, so
    each is checked directly."""

    def test_no_cli_declares_a_budget_flag(self):
        self.assertNotIn('"-b"', CLI_PY)
        self.assertNotIn("--budget", CLI_PY)

    def test_spawn_rejects_a_budget_flag(self):
        # Run the real CLI: a flag that silently parses is worse than none.
        p = subprocess.run([sys.executable, "cli.py", "cell", "spawn", "sonnet",
                            "beta", "apidocs", "-b", "3"],
                           cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("No such option: -b", p.stderr)

    def test_no_spawn_path_puts_a_budget_in_the_environment(self):
        # The env is how a budget used to reach run_cell.sh. Reading it back
        # out of cell.env (a recorded fact) is fine; setting it is not.
        self.assertNotIn("ATTEMPT_BUDGET=str(", OPS_PY)
        self.assertNotIn("ATTEMPT_BUDGET={", OPS_PY)

    def test_no_queue_spec_carries_a_budget(self):
        # the CLI says "budget" about respawns and the exhausted verdict; a
        # budget field or argument is what must not exist
        self.assertNotIn("budget=", CLI_PY)
        self.assertNotIn('"budget":', CLI_PY)
        self.assertNotIn("args.budget", OPS_PY)
        self.assertNotIn('spec.get("budget"', OPS_PY)
        self.assertNotIn('st["budget"] if', OPS_PY)

    def test_a_cell_records_the_constant_it_ran_under(self):
        # Recorded, not configured — the ledger's trace checker reads it back
        # from cell.env to bound attempt numbers.
        self.assertIn("ATTEMPT_BUDGET={cfg.get('ATTEMPT_BUDGET', 10)}", PREPARE)
        self.assertNotIn("${ATTEMPT_BUDGET:-", PREPARE)


class TestTheResumeExceptionIsGone(unittest.TestCase):
    """With the budget fixed, an exhausted cell is exhausted for good — which
    is what lets sealing have no unseal."""
