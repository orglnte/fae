"""The RUNNING table of the fleet console: GATE is the one gate column
(starred while a verify is in flight, an X after the star when that
arrangement's e2e failed), LAST REP carries the block reason while the cell
waits on a holder and its stage history otherwise, BLOCK is YES for a
waiting phase and NO for a working one."""
import io
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs


def _state(cid, phase, **over):
    s = dict(cid=cid, agent=cid.split("_")[0], agent_model="5", state="RUNNING",
             why="", att=2, budget=10, live="-", shape="1/6", hist="scaling gate=BSB",
             detail="", variant="alpha_apidocs", task="T1", rep=1,
             green_at=None, taint=False, alerts_open=0, alert_last="", noedit=0)
    s.update(over)
    return s, {"phase": phase, "phase_age": 60}


class TestTheRunningTable(unittest.TestCase):
    def _render(self, rows):
        states = [s for s, _ in rows]
        hbs = {s["cid"]: hb for s, hb in rows}
        with mock.patch.object(runs.render.state, "all_states", return_value=(states, {}, [])), \
             mock.patch.object(runs.render.state, "loop_parents", return_value={}), \
             mock.patch.object(runs.render.state, "heartbeat",
                               side_effect=lambda ws: hbs[Path(ws).name]), \
             mock.patch.object(runs.render.zombies, "find_zombies", return_value=[]), \
             mock.patch.object(runs.render, "queued_summary", return_value=[]), \
             mock.patch.object(runs.queues_module.Queues, "weekly_line", return_value=""), \
             mock.patch.object(runs.render.common, "definition") as d:
            d.return_value.matrix = {"alpha": ["apidocs"]}
            d.return_value.tech_of = lambda arm: arm
            return runs.render.render(running_only=True)

    def test_the_columns(self):
        working = _state("sonnet_high_alpha_apidocs_T1_r1", "verify", shape="2/6*")
        waiting = _state("haiku_high_alpha_apidocs_T1_r2", "arm-lock",
                         detail="held by sonnet_high_alpha_apidocs_T1_r1")
        out = self._render([working, waiting])
        hdr = next(l for l in out.splitlines() if l.lstrip().startswith("ID"))
        self.assertNotIn("LIVE", hdr)
        self.assertEqual(hdr.split()[-4:], ["GATE", "LAST", "REP", "BLOCK"])
        rows = {l.split()[1]: l for l in out.splitlines() if l.lstrip()[:1].isdigit()}
        w = rows["sonnet_high_alpha_apidocs_T1_r1"]
        self.assertIn("2/6*", w)
        self.assertIn("scaling gate=BSB", w)
        self.assertTrue(w.rstrip().endswith("NO"))
        b = rows["haiku_high_alpha_apidocs_T1_r2"]
        self.assertIn("blocked by 2", b)
        self.assertNotIn("scaling gate=BSB", b)
        self.assertTrue(b.rstrip().endswith("YES"))


class TestTheInFlightE2E(unittest.TestCase):
    def test_a_failed_e2e_reads_as_an_x(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self.assertFalse(runs.state._e2e_failed(ws))
            (ws / "metrics.json").write_text(json.dumps({"e2e_pass": 7, "e2e_total": 7}))
            self.assertFalse(runs.state._e2e_failed(ws))
            (ws / "metrics.json").write_text(json.dumps({"e2e_pass": 5, "e2e_total": 7}))
            self.assertTrue(runs.state._e2e_failed(ws))


if __name__ == "__main__":
    unittest.main()
