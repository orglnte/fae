"""fae/metrics.py — the one reader of metrics.json for validation and scoring.

A count written as the text it was parsed from must reach the rules and the
results table as a number: a string there crashed the taint rules and made the
table drop the cell from its load-error rate without a word.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs

from fae import metrics
from fae.cell.cell import Cell


def _ws(doc):
    d = Path(tempfile.mkdtemp())
    (d / "metrics.json").write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
    return d


class TestTheReader(unittest.TestCase):

    def test_counts_written_as_text_are_numbers(self):
        m = metrics.read(_ws({"load_total": "28114", "load_errors": "0",
                              "mount_delay_s": "53.5", "e2e_pass": 7}))
        self.assertEqual((m["load_total"], m["load_errors"], m["mount_delay_s"], m["e2e_pass"]),
                         (28114, 0, 53.5, 7))
        self.assertIsInstance(m["load_total"], int)

    def test_a_count_that_is_not_a_number_is_left_for_the_rules(self):
        self.assertEqual(metrics.read(_ws({"load_total": "n/a"}))["load_total"], "n/a")

    def test_other_fields_are_untouched(self):
        m = metrics.read(_ws({"stage_failed": "0", "cell_id": "x_r1", "load_total": None}))
        self.assertEqual((m["stage_failed"], m["cell_id"], m["load_total"]), ("0", "x_r1", None))

    def test_a_missing_or_broken_file_is_empty(self):
        self.assertEqual(metrics.read(Path(tempfile.mkdtemp())), {})
        self.assertEqual(metrics.read(_ws("{not json")), {})
        self.assertEqual(metrics.read(_ws("[1, 2]")), {})


class TestTheReadersUseIt(unittest.TestCase):

    def test_the_cell_reads_numbers(self):
        ws = _ws({"load_total": "10", "load_errors": "1"})
        self.assertEqual(Cell(ws.name, workspaces=ws.parent).read_metrics(),
                         {"load_total": 10, "load_errors": 1})

    def test_validation_hands_the_rules_numbers(self):
        ws = _ws({"load_total": "28114", "load_errors": "3"})
        seen = {}

        def rules(ws, root, m, *rest):
            seen.update(m)
            return [], [], {}

        definition = mock.Mock(taint_rules=rules)
        with mock.patch.object(runs.common, "definition", return_value=definition), \
             mock.patch.object(runs.common, "WS", ws.parent), \
             mock.patch.object(runs.common, "LOCKS", ws.parent / ".locks"):
            runs.taint._validate_cell(ws)
        self.assertEqual((seen["load_total"], seen["load_errors"]), (28114, 3))


if __name__ == "__main__":
    unittest.main()
