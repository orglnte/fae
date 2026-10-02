"""The cell, as a package.

    Cell            identity, lifecycle, judgement — the thing spawn() starts
    Ctx / Verdict   the boundary to the experiment's verifier (run_verifier)
    Arena           the fd arena — the slot files, open on reserved fds
    fsm             the state machine, pure and testable without a workspace
    checkpoints     per-attempt provenance — git tree hashes
    surface         the authorable surface — manifest, seal, heal, check

Split this way because these are answerable separately: the FSM can be read
against .tla/Runs.tla without a filesystem, and the verify is the part that
needs an infra. Sealing is a property of a cell, so it lives on Cell.

Usage from anywhere in the repo:

    from fae.cell import Cell
    Cell("sonnet_high_python_apidocs_T1_r1").verdict
"""
from .arena import Arena
from .cell import ATTEMPT_BUDGET, Cell, Halt, VerifyResult
from .checkpoints import Checkpoints
from .surface import Surface
from .fsm import (ENABLED, PHASE_TO_LOOP, IllegalTransition, Loop, Phase,
                  Sealed, State, T)
from .verify import Ctx, Verdict, run_verifier

SEAL_MARKER = Cell.SEAL_MARKER
IMPL = Cell.IMPL
SEAL_EXIT = Cell.SEAL_EXIT

__all__ = ["Cell", "Halt", "Ctx", "Verdict", "run_verifier", "VerifyResult", "Arena", "Checkpoints", "Surface",
           "Loop", "Phase", "T", "State", "PHASE_TO_LOOP", "ENABLED",
           "Sealed", "IllegalTransition", "ATTEMPT_BUDGET",
           "SEAL_MARKER", "SEAL_EXIT"]
