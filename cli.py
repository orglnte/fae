#!/usr/bin/env python3
"""The operator's entry point at a root: `python3 cli.py …` is `fae …`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fae.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
