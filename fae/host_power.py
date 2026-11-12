"""Whether the host is in Low Power Mode, read from the OS.

A verify is timed against the service under test, and Low Power Mode slows
the CPU. macOS says so in `pmset -g` (`lowpowermode 1`); other systems have
no such mode to read: they answer no, and `readable()` says so."""
from __future__ import annotations

import re
import subprocess
import sys

FIX = "turn Low Power Mode off (`sudo pmset -a lowpowermode 0`)"
UNREAD = ("this system's power-saving mode is not read (macOS only): make sure "
          "the host is not saving power")


def readable() -> bool:
    """Whether this system's power mode can be read at all."""
    return sys.platform == "darwin"


def parse_pmset(text) -> bool:
    """Whether `pmset -g` output says Low Power Mode is on."""
    m = re.search(r"^\s*lowpowermode\s+(\d+)", text if isinstance(text, str) else "", re.M)
    return bool(m) and m.group(1) != "0"


def _pmset() -> str:
    """`pmset -g` output; "" where there is none to read."""
    if not readable():
        return ""
    try:
        return subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def low_power_mode() -> bool:
    """Whether the host is in Low Power Mode now."""
    return parse_pmset(_pmset())
