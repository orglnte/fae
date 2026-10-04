# Concepts to fix

Concepts of [`CONCEPTS-repo-fae.md`](CONCEPTS-repo-fae.md) that do not yet
behave or read as an operator would expect. A
[dark concept](https://essenceofsoftware.com/posts/dark-concepts/) is one
whose actual effect diverges from what its user expects, or that acts without
telling them.

## 1. Dark concepts

1. **Fingerprint.**
   1. What it does: a running cell pins the fingerprint when its process
      starts and compares it at every verify. Editing any file it covers (the
      experiment tree, any file in it including docs, the declared
      fingerprint trees, the engine's `fae/cell/` and `fae/experiment/`,
      scoring included) refunds that cell's next verify (stage
      `harness-fp`), halts the cell and discards the agent's edits of that
      attempt; supervision then respawns it, the respawn counts toward the
      cell's repairs (`MAX_RESPAWNS`, 3 by default), and the new process pins
      the new fingerprint.
   2. Why dark: an edit to a scoring script or a markdown file reads as
      unrelated to running cells, yet it costs each of them an attempt's
      agent work and one of its repairs.
   3. How the operator finds out: a `HALT … harness-fp` line in the cell's
      ledger and the respawn in the reconcile log; nothing warns before the
      edit.
   4. Remedy: warn on any covered-file change while cells run, or take
      scoring and docs out of the fingerprint.
2. **Refunded attempts.**
   1. What it does: a refunded verify consumes no attempt, and the score and
      the scoreboard count charged attempts only, including the agent's
      time.
   2. Why dark: a cell can spend several rounds of agent work and verify
      time on refunds and still show "green at 1".
   3. How the operator finds out: validation warns only at three or more
      uncharged runs, or two of the same stage; otherwise the archive under
      `arrangements/`.
   4. Remedy: a refunds column per cell in the score and the scoreboard.
3. **Scoreboard.**
   1. What it does: the table is built from the cells that have a
      `score.json`. Supervision validates finished cells by itself, but
      scoring runs only on `results score`.
   2. Why dark: a finished, validated cell that was never scored is absent
      from the table, and nothing says the table is short.
   3. How the operator finds out: they don't, unless they count finished
      cells against the table's rows. The staleness check covers only
      records that exist.
   4. Remedy: the aggregate counts finished cells without a `score.json`
      and refuses, or prints the count.
4. **Lifecycle log.**
   1. What it does: the replay starts at the log's last `EPOCH` block;
      everything before it is neither replayed nor reported.
   2. Why dark: `tla_verify --live-trace OK` reads as "the whole run
      conformed", but covers only the events since the last reset.
   3. How the operator finds out: they don't; the OK line gives the event
      count, not the start.
   4. Remedy: the OK line names the `EPOCH` stamp it replayed from.

Checked and left out:
1. A sealed cell refusing a requeue, parked lanes, cooldowns and budget
   holds: each is printed or tagged in `status`.
2. The host-sleep void and the store re-bench: they belong to an
   experiment's verifier, not to the engine.

## 2. Concepts that miss a quality

1. **Lane hold** is one mechanism for three causes: the operator's park,
   the budget hold (which parks the lane through the same directory), and
   a provider cooldown. `status` tags the first two apart, but the state
   on disk is one.
2. **Fingerprint** is not user-facing as a choice, yet it acts on the user's
   cells (§1.1).

## 3. Conceptual entropy

Names that collide today: one meaning under several names, or one name
with several meanings.

1. **Attempt / iteration.** The concept is an attempt (`ATTEMPT_BUDGET`,
   attempts to green); the ledger writes `ITER` in `iterations.log`, and
   the scoring code says iterations-to-green.
2. **Arrangement / shape.** `Gate.arrangements`; the ledger's `SHAPE`
   event, the archive's `<shape>` folders and the verify code say shape.
3. **Refunded / void / uncharged / not charged.** One meaning: a failed
   verdict that consumes no attempt (`Verdict.charge = False`, the
   ledger's `void=`, the archive's `refunded` end state,
   `archive.NOT_CHARGED`).
4. **Lock / arm.** A variant file's `[infra] lock` is a cap on live infra;
   the same thing is `Cell.arm`, `arm-<lock>.slots/` and `ARM_SLOTS_<LOCK>`.
5. **Rig.** Three meanings: the measuring apparatus (a "rig fault" is
   refunded), `rig-lock` (the verifier's exclusive lock), and the verify container's
   holdings ("the verify container holds the rig").
6. **Workspace.** The `Workspace` object (a root of cell folders and the
   scheduling plane beside them) and a cell's own folder (`ws`).
7. **Pause / stand-down / halt / stop / kill.** An operator pause, a
   verifier's stand-down, a rig-fault halt, a stop and a kill all leave a
   cell not running; `status` shows a stand-down as paused.
8. **Reconcile / supervise / diagnose / repair.** One supervision pass,
   under four names (the reconcile log, `supervise.py`, `experiment
   diagnose`, `experiment repair`).
9. **Smoke / reference / ref.** Smoke runs a variant's reference; its cells
   carry the agent tag `ref` and the `_smoke` id token.
