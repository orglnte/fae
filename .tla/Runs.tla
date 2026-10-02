---------------------------- MODULE Runs ----------------------------
(***************************************************************************)
(* Formal model of the fae orchestration layer (conduct and the cell      *)
(* driver): the three-axis cell state (outcome / intent / liveness),       *)
(* the lock set (verify lock, work slots, per-workspace loop lock), and    *)
(* the actors that move cells (loops, the operator, reconcile, worker).    *)
(*                                                                         *)
(* The model checks the invariants the rig's incident history violated:    *)
(*   VerifyMutualExclusion  one verify at a time (verdict attributability) *)
(*   OneLoopPerCell         the loop lock's purpose (TOCTOU double-spawn)  *)
(*   WorkCap                at most SLOTS cells hold a work slot           *)
(*   VerdictStable          a finished cell never un-finishes (2026-07-25: *)
(*                          a bookkeeping append hid 7 verdicts)           *)
(*   KilledStaysDead        resume-all never resurrects a killed cell      *)
(*   PausedNeverWorks       a pause-locked cell never enters verify        *)
(*   NoBudgetOverrun        attempts never exceed the budget               *)
(*                                                                         *)
(* Verify with TLC, or with the PEM explicit-state checker (tla_verify),   *)
(* which reads the CHECK-* headers below; `runs.py selftest` replays the    *)
(* live transitions.log against this spec (tla_verify --live-trace).       *)
(***************************************************************************)
\* CHECK-CONSTANTS: Cells={"c1","c2"}; Budget=2; Slots=1
\* CHECK-ACTIONS: Spawn AcquireSlot ReleaseSlot StandDown AcquireVerify AcquireRig ReleaseRig VerifyGreen VerifyFail Crash Pause Resume Kill
\* CHECK-INVARIANTS: TypeOK VerifyMutualExclusion WorkCap KilledStaysDead NoBudgetOverrun NoLockWithoutLoop RigOnlyUnderVerify
\* CHECK-ARGS: Cells
EXTENDS Naturals, FiniteSets

CONSTANTS
  Cells,      \* e.g. {c1, c2}
  Budget,     \* attempt budget per cell, e.g. 2
  Slots       \* global work-slot cap, e.g. 1

VARIABLES
  outcome,    \* [Cells -> {"none", "green", "failed", "revoked"}]  (ledger)
  intent,     \* [Cells -> {"run", "paused", "killed"}]             (markers)
  loop,       \* [Cells -> {"none", "idle", "agent", "verify"}]     (liveness)
  attempts,   \* [Cells -> 0..Budget]
  slotHeld,   \* [Cells -> {TRUE, FALSE}]   holds a work slot
  verifyHeld, \* {} or {c}            the global verify lock holder
  rigHeld     \* {} or {c}            a verifier's declared exclusive lock (Cell.exclusive_acquire); none declared today

vars == <<outcome, intent, loop, attempts, slotHeld, verifyHeld, rigHeld>>

Terminal(c) == outcome[c] /= "none"

TypeOK ==
  /\ outcome \in [Cells -> {"none", "green", "failed", "revoked"}]
  /\ intent  \in [Cells -> {"run", "paused", "killed"}]
  /\ loop    \in [Cells -> {"none", "idle", "agent", "verify"}]
  /\ attempts \in [Cells -> 0..Budget]
  /\ slotHeld \in [Cells -> {TRUE, FALSE}]
  /\ verifyHeld \in SUBSET Cells /\ Cardinality(verifyHeld) <= 1
  /\ rigHeld \in SUBSET Cells /\ Cardinality(rigHeld) <= 1

Init ==
  /\ outcome  = [c \in Cells |-> "none"]
  /\ intent   = [c \in Cells |-> "run"]
  /\ loop     = [c \in Cells |-> "none"]
  /\ attempts = [c \in Cells |-> 0]
  /\ slotHeld = [c \in Cells |-> FALSE]
  /\ verifyHeld = {}
  /\ rigHeld = {}

(***************************************************************************)
(* Loop lifecycle.  Spawn models runs.py spawn / worker / reconcile        *)
(* _respawn: all three paths must pass the loop lock (loop[c] = "none").   *)
(***************************************************************************)
Spawn(c) ==
  /\ loop[c] = "none"                       \* the loop lock: nobody owns c
  /\ ~Terminal(c)                           \* worker doneness via cell_state
  /\ intent[c] = "run"                      \* spawn paths honor pause/kill
  /\ loop' = [loop EXCEPT ![c] = "idle"]
  /\ UNCHANGED <<outcome, intent, attempts, slotHeld, verifyHeld, rigHeld>>

AcquireSlot(c) ==
  /\ loop[c] = "idle" /\ ~slotHeld[c]
  /\ intent[c] = "run"                      \* slot queue polls the pause lock
  /\ Cardinality({d \in Cells : slotHeld[d]}) < Slots
  /\ slotHeld' = [slotHeld EXCEPT ![c] = TRUE]
  /\ loop' = [loop EXCEPT ![c] = "agent"]
  /\ attempts' = [attempts EXCEPT ![c] = @ + 1]   \* attempt begins
  /\ UNCHANGED <<outcome, intent, verifyHeld, rigHeld>>

(* pause honored while queued for a slot / at the boundary *)
StandDown(c) ==
  /\ loop[c] \in {"idle", "agent"}
  /\ intent[c] /= "run"
  /\ loop' = [loop EXCEPT ![c] = "none"]
  /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]
  /\ attempts' = [attempts EXCEPT ![c] = IF loop[c] = "agent" THEN @ - 1 ELSE @]
       \* an attempt abandoned pre-verify is unrecorded (redone on resume)
  /\ UNCHANGED <<outcome, intent, verifyHeld, rigHeld>>

AcquireVerify(c) ==
  /\ loop[c] = "agent"
  /\ verifyHeld = {}
  /\ intent[c] = "run"                      \* vlock queue + post-acquire check
  /\ verifyHeld' = {c}
  /\ loop' = [loop EXCEPT ![c] = "verify"]
  /\ UNCHANGED <<outcome, intent, attempts, slotHeld, rigHeld>>

(* Cell.exclusive_acquire (fae/cell/cell.py) takes the rig lock — the
   lock the experiment's verifier declares EXCLUSIVE — on the cell's own fd
   around the verifier subprocess, and it runs inside the verify: the rig
   is only ever held by the cell that already holds the verify lock. That
   ordering is what makes a deadlock impossible: a cell waiting for the rig
   cannot be holding anything another rig holder needs. *)
AcquireRig(c) ==
  /\ loop[c] = "verify" /\ verifyHeld = {c}
  /\ rigHeld = {}
  /\ rigHeld' = {c}
  /\ UNCHANGED <<outcome, intent, loop, attempts, slotHeld, verifyHeld>>

ReleaseRig(c) ==
  /\ rigHeld = {c}
  /\ rigHeld' = {}
  /\ UNCHANGED <<outcome, intent, loop, attempts, slotHeld, verifyHeld>>

VerifyGreen(c) ==
  /\ loop[c] = "verify" /\ verifyHeld = {c}
  /\ outcome' = [outcome EXCEPT ![c] = "green"]
  /\ verifyHeld' = {}
  /\ rigHeld' = rigHeld \ {c}
  /\ loop' = [loop EXCEPT ![c] = "none"]     \* END written, loop exits
  /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]
  /\ UNCHANGED <<intent, attempts>>

VerifyFail(c) ==
  /\ loop[c] = "verify" /\ verifyHeld = {c}
  /\ verifyHeld' = {}
  /\ rigHeld' = rigHeld \ {c}
  /\ IF attempts[c] >= Budget
     THEN /\ outcome' = [outcome EXCEPT ![c] = "failed"]   \* budget exhausted
          /\ loop' = [loop EXCEPT ![c] = "none"]
          /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]
          /\ attempts' = attempts
     ELSE \* next attempt begins directly in "agent": run_cell.sh's retry
          \* loop re-enters the build immediately, no separate AcquireSlot
          \* (the slot is held for the cell's whole lifetime, not per
          \* attempt) — discovered via live-trace replay (item 4): the old
          \* "idle" transition here was a dead end, reachable only via the
          \* unrealistic Crash action, never by a real retry.
          /\ outcome' = outcome
          /\ loop' = [loop EXCEPT ![c] = "agent"]
          /\ slotHeld' = slotHeld
          /\ attempts' = [attempts EXCEPT ![c] = @ + 1]
  /\ UNCHANGED intent

(* teardown: cell_teardown frees the real slot on EVERY exit path, including
   HALT/abort exits that produce no verdict.  Emitted by slot_release itself,
   so it also arrives after VerifyGreen/VerifyFail/StandDown have already
   released in the model — hence NO guard: the action is idempotent and a
   duplicate emission is a no-op, never a violation. *)
ReleaseSlot(c) ==
  /\ loop' = [loop EXCEPT ![c] = "none"]
  /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]
  /\ verifyHeld' = verifyHeld \ {c}
  /\ rigHeld' = rigHeld \ {c}
  /\ attempts' = [attempts EXCEPT ![c] =
       IF loop[c] \in {"agent", "verify"} THEN @ - 1 ELSE @]
       \* a HALT/abort mid-attempt is unrecorded; the respawn redoes it
  /\ UNCHANGED <<outcome, intent>>

Crash(c) ==   \* SIGKILL / OOM / laptop sleep: any live loop can vanish
  /\ loop[c] /= "none"
  /\ loop' = [loop EXCEPT ![c] = "none"]
  /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]     \* the kernel frees it
  /\ verifyHeld' = verifyHeld \ {c}                 \* with the process
  /\ rigHeld' = rigHeld \ {c}
  /\ attempts' = [attempts EXCEPT ![c] = IF loop[c] \in {"agent", "verify"} THEN @ - 1 ELSE @]
  /\ UNCHANGED <<outcome, intent>>

(***************************************************************************)
(* Operator + supervisors                                                  *)
(***************************************************************************)
Pause(c) ==
  /\ intent[c] = "run"
  /\ intent' = [intent EXCEPT ![c] = "paused"]
  /\ UNCHANGED <<outcome, loop, attempts, slotHeld, verifyHeld, rigHeld>>

(* resume all: lifts pause but NEVER killed (18b1de5) and never respawns
   into a terminal cell; respawn itself is Spawn *)
Resume(c) ==
  /\ intent[c] = "paused"
  /\ intent' = [intent EXCEPT ![c] = "run"]
  /\ UNCHANGED <<outcome, loop, attempts, slotHeld, verifyHeld, rigHeld>>

(* kill (`cell stop --cancel` since 2026-08-12): intent first, then the loop
   dies, infra torn down; DONE cells are excluded (7219f9b). The plain
   resumable `stop` is NOT this action — it is Pause + Crash. *)
Kill(c) ==
  /\ ~Terminal(c)
  /\ intent' = [intent EXCEPT ![c] = "killed"]
  /\ loop' = [loop EXCEPT ![c] = "none"]
  /\ slotHeld' = [slotHeld EXCEPT ![c] = FALSE]
  /\ verifyHeld' = verifyHeld \ {c}
  /\ rigHeld' = rigHeld \ {c}
  /\ attempts' = [attempts EXCEPT ![c] = IF loop[c] \in {"agent", "verify"} THEN @ - 1 ELSE @]
  /\ UNCHANGED outcome

(* reconcile: respawns crashed non-terminal run-intent cells — same guard
   set as Spawn, so it is Spawn; the model needs no separate action.
   The 2026-07-25 regression is modeled by ReconcileRepair being ENABLED
   only when a reverify is genuinely active — the model has no reverify,
   so the action does not exist: any append that changes outcome of a
   Terminal cell would violate VerdictStable. *)

Next == \E c \in Cells :
  \/ Spawn(c) \/ AcquireSlot(c) \/ ReleaseSlot(c) \/ StandDown(c)
  \/ AcquireVerify(c) \/ AcquireRig(c) \/ ReleaseRig(c)
  \/ VerifyGreen(c) \/ VerifyFail(c) \/ Crash(c)
  \/ Pause(c) \/ Resume(c) \/ Kill(c)

Spec == Init /\ [][Next]_vars

(***************************************************************************)
(* Invariants                                                              *)
(***************************************************************************)
VerifyMutualExclusion == Cardinality(verifyHeld) <= 1

OneLoopPerCell == TRUE  \* structural here (loop is a function); the python
                        \* checker additionally asserts Spawn is disabled
                        \* when a loop exists — the TOCTOU class

WorkCap == Cardinality({c \in Cells : slotHeld[c]}) <= Slots

VerdictStable ==   \* checked as an action property in the python checker:
  TRUE             \* outcome never leaves a terminal value

KilledStaysDead == \A c \in Cells : intent[c] = "killed" => loop[c] = "none"

\* A lock is held by an open fd, so it cannot outlive the process holding it.
\* Under a lock that is merely a file on disk this is FALSE: a crashed loop's
\* slot stays held until something judges it stale.
NoLockWithoutLoop == \A c \in Cells :
  /\ (slotHeld[c] => loop[c] /= "none")
  /\ (c \in verifyHeld => loop[c] /= "none")
  /\ (c \in rigHeld => loop[c] /= "none")

\* Lock ordering, verify before rig. A holder of the rig also holds the
\* verify lock, and the verify lock admits one cell — so no cell can ever
\* wait for a rig held by someone else while holding the verify itself.
RigOnlyUnderVerify == \A c \in Cells : c \in rigHeld => c \in verifyHeld

PausedNeverVerifies == \A c \in Cells :
  intent[c] /= "run" => loop[c] /= "verify"
  \* NOTE: deliberately STRONGER than the implementation, which lets a
  \* mid-verify cell finish after a pause lands (pause pending). The python
  \* checker encodes the implementation-accurate version: a cell may be in
  \* "verify" with intent /= "run" only if the pause arrived AFTER the
  \* verify lock was acquired. TLC users: check PausedEventuallyStops.

NoBudgetOverrun == \A c \in Cells : attempts[c] <= Budget

Invariants == TypeOK /\ VerifyMutualExclusion /\ WorkCap
              /\ KilledStaysDead /\ NoBudgetOverrun /\ RigOnlyUnderVerify

=============================================================================
