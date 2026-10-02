"""fae/cell/infra/base.host_ports: a cell's ports on the host's loopback,
one per role, all from the same slot of their ranges."""
import unittest

from _ctx import ROOT  # noqa: F401

from fae.cell.infra.base import cksum, host_ports


class TestHostPorts(unittest.TestCase):
    RANGES = {"api": range(30000, 30010), "cache": range(31000, 31010)}

    def test_every_role_takes_the_same_slot(self):
        p = host_ports("cell-a", self.RANGES)
        slot = cksum("cell-a") % 10
        self.assertEqual(p, {"api": 30000 + slot, "cache": 31000 + slot})

    def test_the_shortest_range_bounds_the_slots(self):
        p = host_ports("cell-b", {"api": range(30000, 30100), "pd": range(32000, 32003)})
        self.assertIn(p["pd"], range(32000, 32003))
        self.assertEqual(p["api"] - 30000, p["pd"] - 32000)

    def test_it_is_the_same_for_the_same_cell(self):
        self.assertEqual(host_ports("cell-a", self.RANGES), host_ports("cell-a", self.RANGES))


if __name__ == "__main__":
    unittest.main()
