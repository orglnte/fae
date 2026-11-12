"""Whether the host is in Low Power Mode, read from `pmset -g`."""
import unittest
from unittest import mock

import _ctx  # noqa: F401  (sys.path)
from fae import host_power

MODE_ON = """Currently in use:
 lowpowermode         1
 standby              1
 powernap             0
"""
MODE_OFF = MODE_ON.replace("lowpowermode         1", "lowpowermode         0")


class TestParsePmset(unittest.TestCase):

    def test_on(self):
        self.assertTrue(host_power.parse_pmset(MODE_ON))

    def test_off(self):
        self.assertFalse(host_power.parse_pmset(MODE_OFF))

    def test_a_host_that_does_not_list_the_mode(self):
        self.assertFalse(host_power.parse_pmset(" standby 1\n hibernatemode 3\n"))

    def test_no_output(self):
        self.assertFalse(host_power.parse_pmset(""))
        self.assertFalse(host_power.parse_pmset(None))


class TestLowPowerMode(unittest.TestCase):

    def test_it_reads_what_the_os_says_now(self):
        with mock.patch.object(host_power, "_pmset", return_value=MODE_ON):
            self.assertTrue(host_power.low_power_mode())
        with mock.patch.object(host_power, "_pmset", return_value=MODE_OFF):
            self.assertFalse(host_power.low_power_mode())


if __name__ == "__main__":
    unittest.main()
