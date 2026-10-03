"""The cell lifecycle, as a state machine — the per-cell projection of
.tla/Runs.tla.

SEPARATED FROM THE CELL ON PURPOSE. The FSM is a pure function of its own
state: no files, no subprocesses, no clock. That is what lets the transition
table be read against the spec line by line, and tested without a workspace.
`Cell` composes it; nothing here knows what a workspace is.

TWO VOCABULARIES, ONE MAPPING. `Loop` is what the model reasons about — four
coarse states. `Phase` is what an operator needs — where a cell WAITS or WORKS.
A cell is admitted holding its slots, so every phase after admission is at
least TLA-`agent`; a cell in `verify-lock` is NOT `verify` (it is queued, not
verifying). PHASE_TO_LOOP is
the only place those two are related, and it is total: a phase with no loop
would let the console and the model describe the same cell differently.

ONLY PER-CELL GUARDS LIVE HERE. `WorkCap`, `VerifyMutualExclusion` and
`RigOnlyUnderVerify` quantify over the whole fleet; one cell cannot see them.
They are enforced by the flocks — the only thing that can — and checked
globally by tla_verify.
"""
from __future__ import annotations

from enum import Enum


class Sealed(Exception):
    """Raised when a finished result is asked to change."""


class IllegalTransition(Exception):
    """The local projection of Runs.tla said no."""


class Loop(str, Enum):
    """The TLA+ vocabulary. Coarse on purpose: these are the states the model
    reasons about, and adding one means changing .tla/Runs.tla."""
    NONE = "none"
    IDLE = "idle"
    AGENT = "agent"
    VERIFY = "verify"


class Phase(str, Enum):
    """The heartbeat vocabulary — where a cell waits or works."""
    SETUP = "setup"
    AGENT = "agent"
    LIMIT = "limit"
    VERIFY_LOCK = "verify-lock"
    VERIFY = "verify"


PHASE_TO_LOOP = {
    Phase.SETUP: Loop.AGENT,         # admitted: holds its slots while its infra comes up
    Phase.AGENT: Loop.AGENT,
    Phase.LIMIT: Loop.AGENT,         # waiting out an API wall, attempt intact
    Phase.VERIFY_LOCK: Loop.AGENT,   # queued for the verify; not verifying yet
    Phase.VERIFY: Loop.VERIFY,
}


class T(str, Enum):
    """Transitions, named exactly as .tla/Runs.tla names them — the live-trace
    replay matches on these strings."""
    ADMIT = "Admit"
    RELEASE_SLOT = "ReleaseSlot"
    STAND_DOWN = "StandDown"
    ACQUIRE_VERIFY = "AcquireVerify"
    ACQUIRE_RIG = "AcquireRig"
    RELEASE_RIG = "ReleaseRig"
    VERIFY_GREEN = "VerifyGreen"
    VERIFY_FAIL = "VerifyFail"
    CRASH = "Crash"
    PAUSE = "Pause"
    RESUME = "Resume"
    KILL = "Kill"


# Records in transitions.log that end a cell's loop, and those that leave it as
# it was: the last loop-affecting record says whether a loop is still owed.
LOOP_CLEARED_BY = frozenset({"Crash", "Kill", "ReleaseSlot", "VerifyGreen", "StandDown",
                             "EPOCH", "Retire"})
LOOP_UNCHANGED_BY = frozenset({"Pause", "Resume"})


class State:
    """The per-cell projection of the model's variables."""

    def __init__(self):
        self.loop = Loop.NONE
        self.intent = "run"          # run | paused | killed
        self.attempts = 0
        self.slot_held = False
        self.verify_held = False
        self.rig_held = False
        self.outcome = None          # None | green | failed | revoked

    def __repr__(self):
        return (f"State(loop={self.loop.value} intent={self.intent} "
                f"attempts={self.attempts} slot={self.slot_held} "
                f"verify={self.verify_held} rig={self.rig_held})")


# Enabling conditions, transcribed from the spec. Fleet-wide conjuncts (the
# slot cap, verify mutual exclusion, the rig set) are deliberately absent.
ENABLED = {
    T.ADMIT: lambda s: s.loop is Loop.NONE and s.intent == "run"
                       and s.outcome is None and not s.slot_held,
    T.ACQUIRE_VERIFY: lambda s: s.loop is Loop.AGENT and not s.verify_held
                                and s.intent == "run",
    T.ACQUIRE_RIG: lambda s: s.loop is Loop.VERIFY and not s.rig_held,
    T.RELEASE_RIG: lambda s: s.rig_held,
    T.VERIFY_GREEN: lambda s: s.loop is Loop.VERIFY,
    T.VERIFY_FAIL: lambda s: s.loop is Loop.VERIFY,
    T.RELEASE_SLOT: lambda s: s.loop is not Loop.NONE,
    T.STAND_DOWN: lambda s: s.loop in (Loop.IDLE, Loop.AGENT)
                            and s.intent != "run",
    T.CRASH: lambda s: s.loop is not Loop.NONE,
    T.PAUSE: lambda s: s.intent == "run",
    T.RESUME: lambda s: s.intent == "paused",
    T.KILL: lambda s: s.intent != "killed",
}


def fire(s, t):
    """Apply one transition. Mirrors the primed variables in the spec; this is
    the only function that writes to a State."""
    if t is T.ADMIT:
        s.slot_held, s.loop, s.attempts = True, Loop.AGENT, s.attempts + 1
    elif t is T.ACQUIRE_VERIFY:
        s.verify_held, s.loop = True, Loop.VERIFY
    elif t is T.ACQUIRE_RIG:
        s.rig_held = True
    elif t is T.RELEASE_RIG:
        s.rig_held = False
    elif t is T.VERIFY_GREEN:
        s.outcome, s.loop = "green", Loop.NONE
        s.slot_held = s.verify_held = s.rig_held = False
    elif t is T.VERIFY_FAIL:
        s.loop, s.verify_held = Loop.AGENT, False
    elif t in (T.RELEASE_SLOT, T.CRASH):
        # An attempt abandoned before it was judged is not an attempt: the
        # respawn redoes it. Same arithmetic as the spec, or the model and the
        # ledger disagree about how many attempts a cell has spent.
        if s.loop in (Loop.AGENT, Loop.VERIFY):
            s.attempts -= 1
        s.loop = Loop.NONE
        s.slot_held = s.verify_held = s.rig_held = False
    elif t is T.STAND_DOWN:
        if s.loop is Loop.AGENT:
            s.attempts -= 1
        s.loop, s.slot_held = Loop.NONE, False
    elif t is T.PAUSE:
        s.intent = "paused"
    elif t is T.RESUME:
        s.intent = "run"
    elif t is T.KILL:
        s.intent = "killed"
    return s


def step(s, t):
    """Check, then apply. Raises IllegalTransition rather than silently
    tolerating a transition the model does not admit — a log that records
    transitions which did not happen is worse than no log."""
    t = T(t)
    if not ENABLED[t](s):
        raise IllegalTransition(f"{t.value} is not enabled: {s}")
    return fire(s, t)
