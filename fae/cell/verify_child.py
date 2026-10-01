"""The verify container's entry point: `python3 -m fae.cell.verify_child
--ctx F --out F`. A module of its own so the package's import of `verify`
and the child's `__main__` are never two copies of the same module."""
import sys

from .verify import main

sys.exit(main())
