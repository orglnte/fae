"""The rig's own health and maintenance verbs — not experiment semantics.

selftest (cross-language invariants + TLA+ live-trace conformance), the
zombie-reap CLI wrapper, trace-reset (transitions.log archive/reseed),
infra (per-arm preflight), smoke (pipeline check through the driver,
no agent) and prepare (seed the matrix's workspaces, launch nothing) — the
verbs the plan's Milestone 2 clusters left in runs.py because none of them
are entangled with the scheduler/backlog/scoring clusters; each is a
standalone diagnostic or maintenance action an operator runs by hand.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import ujson as json

from fae.driver import common
from fae.driver import ops
from fae.driver import state
from fae.driver import zombies
from fae.driver.common import (
    ROOT, cell_id, ledger, mutex,
    parse_cell_id,
)


def tla_verify_path():
    """The TLA+ trace checker: $FAE_TLA_VERIFY, else `tla_verify` on PATH;
    None when neither names a file."""
    p = os.environ.get("FAE_TLA_VERIFY") or shutil.which("tla_verify")
    return p if p and Path(p).is_file() else None


def resolve_seed_doc(treatment, condition):
    """The doc fae/cell/prepare.py would actually seed, by the same rule it uses."""
    cls = common.definition().variant(treatment)
    if cls is None:
        return None
    docs = common.definition().docs_of(treatment)
    seed = cls.seed_root()
    specific = seed / f"any.{docs}.{condition}.api.md"
    return specific if specific.is_file() else seed / f"any.{docs}.api.md"


from fae import paths as _paths  # noqa: E402

TLA_DIR = _paths.ENGINE.parent / ".tla"      # the engine repo's model
CONFORMANCE_SINCE = TLA_DIR / "conformance-since"
_ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def conformance_since():
    """['--since', <instant>] for tla_verify, or [] when no cutoff is declared.

    A cutoff in the future would judge nothing, which is a green check that
    proves nothing — refused rather than honoured.
    """
    try:
        body = CONFORMANCE_SINCE.read_text()
    except OSError:
        return []
    vals = [l.strip() for l in body.splitlines()
            if l.strip() and not l.startswith("#")]
    if not vals:
        return []
    since = vals[-1]
    if not _ISO_Z.match(since):
        print(f"  (ignoring {CONFORMANCE_SINCE.name}: '{since}' is not "
              f"YYYY-MM-DDTHH:MM:SSZ — judging the whole trace)")
        return []
    if since > f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}":
        print(f"  (ignoring {CONFORMANCE_SINCE.name}: {since} is in the "
              f"future, which would judge nothing — judging the whole trace)")
        return []
    return ["--since", since]


def selftest(args):
    """Cheap invariants that guard the cross-language seams: bash and python
    must produce identical cell ids, and the ledger library must agree with a
    fresh parse on every terminal workspace."""
    fails = 0
    # The one cell_id: the driver's entry points import it rather than
    # re-encoding the format (tests/test_cell_id.py pins the wiring).
    # critical harness functions: a bad edit that deletes one turns every
    # verify into recorded attempt failures (2026-07-25, twice) — cheaper to
    # catch here than in burned budget. All Python now; the rig has no bash
    # left except the frozen bring-up scripts, which are not importable.
    sys.path.insert(0, str(ROOT))
    from fae.cell import rig as _rig, verify as _verify, variants as _tr
    from fae.cell import prepare as _prep, config as _config
    for mod, names in ((_rig, ("fp", "free_port_from")),
                       (_verify, ("run_verifier", "call", "run_in_thread")),
                       (mutex, ("pause_requested", "open_lock", "try_fd", "wait_fds")),
                       (_prep, ("prepare", "seed_skeleton", "safe_wipe")),
                       (_config, ("load", "opencode_key_file", "stage_agent"))):
        for name in names:
            if not callable(getattr(mod, name, None)):
                print(f"FAIL critical harness function missing: "
                      f"{getattr(mod, '__name__', mod)}.{name}"); fails += 1
    minted_or_retired = set(common.definition().matrix) | set(common.definition().retired)
    if set(_tr.registry()) != minted_or_retired:
        print(f"FAIL the variant registry knows {sorted(_tr.registry())}, the matrix "
              f"and RETIRED have {sorted(minted_or_retired)}"); fails += 1
    # EFFORT/SMOKE: fae/driver/common.py's cell_id() defaults to effort="high",
    # smoke=False, and every internal caller (ops.spawn, queue.enqueue,
    # render.queued_summary) leaves those defaults alone, while the driver
    # (fae/cell/__main__.py) derives the prefix from the INHERITED
    # environment. Run anything with EFFORT=medium or SMOKE=1 and the two
    # disagree, so every doneness / pause / duplicate-loop check inspects a
    # workspace that does not exist. Changing the derivation mid-study is the
    # riskier move (it touches every cid the orchestrator computes), so this
    # fails LOUDLY instead — operator decision 2026-07-30.
    _eff = os.environ.get("EFFORT", "high")
    if _eff != "high" or os.environ.get("SMOKE"):
        print(f"FAIL EFFORT/SMOKE mismatch: EFFORT={_eff!r} "
              f"SMOKE={os.environ.get('SMOKE')!r} — the orchestrator computes "
              f"cids as 'high'/non-smoke while the driver honours the "
              f"environment, so "
              f"the two disagree about every workspace name. Unset them, or fix "
              f"cell_id's callers first.")
        fails += 1
    # The experiment's own pins on its rules (the mount oracle, the corpus
    # consistency of its greens) — through its declared selftest verb.
    _selftest = common.definition().verbs.get("selftest")
    for f in (_selftest(common.WS) if _selftest else []):
        print(f"FAIL {f}"); fails += 1
    # Seed docs ARE the independent variable. The prepare falls back to the
    # arm's base doc without complaint when a condition doc is missing, so a
    # deleted/renamed file silently changes a cell's information condition, and
    # a truncated one changes it with no filename change at all.
    for (treatment, condition), (want_name, min_lines) in sorted(common.definition().seed_docs.items()):
        got = resolve_seed_doc(treatment, condition)
        if got is None or not got.is_file():
            print(f"FAIL seed doc missing for {treatment}/{condition}: "
                  f"expected {want_name}"); fails += 1
            continue
        if got.name != want_name:
            print(f"FAIL seed doc for {treatment}/{condition} resolved to "
                  f"{got.name}, expected {want_name} — a missing condition doc "
                  f"falls back to the base doc SILENTLY"); fails += 1
            continue
        n = len(got.read_text(errors="replace").splitlines())
        if n < min_lines:
            print(f"FAIL seed doc {got.name} for {treatment}/{condition} is "
                  f"{n} lines, below the {min_lines} floor (truncated?)")
            fails += 1
    for treatment, conditions in common.definition().matrix.items():
        for condition in conditions:
            if (treatment, condition) not in common.definition().seed_docs:
                print(f"FAIL matrix has {treatment}/{condition} but "
                      f"SEED_DOCS does not pin its doc"); fails += 1
    for ws in sorted(common.WS.iterdir()):
        if not ws.is_dir() or not parse_cell_id(ws.name):
            continue
        st = state.cell_state(ws, {}, set())
        if st and st["state"] == "DONE" and st["why"] != "cancelled":
            L = ledger.parse(ws)
            want = {"green": "green", "failed": "failed", "revoked": "revoked"}[st["why"]]
            if L["verdict"] != want:
                print(f"FAIL {ws.name}: cell_state={st['why']} ledger={L['verdict']}")
                fails += 1
    # TLA+ live-trace conformance (item 4): fleet-wide periodic check, unlike
    # the per-attempt --trace already wired at verify-end. Absence tolerated
    # (no events yet, or a fresh checkout with no transitions.log) — this is
    # cheap defense-in-depth, not a required artifact.
    tla_verify = tla_verify_path()
    if tla_verify is None:
        print("  live-trace conformance skipped: no tla_verify "
              "(set FAE_TLA_VERIFY or put tla_verify on PATH)")
    elif common.TRANSITIONS_LOG.exists() and common.TRANSITIONS_LOG.stat().st_size > 0:
        spec = sorted(TLA_DIR.glob("*.tla"))
        # The checker's constants must match the FLEET's configuration, not the
        # operator shell: an unset WORK_SLOTS here would replay the fleet
        # against a smaller slot cap and report legal holders as violations.
        from fae.cell import config as _cellcfg
        _slots = str(_cellcfg.load(ROOT).values.get("WORK_SLOTS") or 8)
        r = subprocess.run(["python3", tla_verify, "--live-trace", str(common.TRANSITIONS_LOG)]
                            + ([str(spec[0])] if spec else [])
                            + conformance_since(),
                            cwd=ROOT, capture_output=True, text=True,
                            env=dict(os.environ, WORK_SLOTS=_slots))
        if r.returncode != 0:
            print(f"FAIL live-trace conformance:\n{r.stdout}{r.stderr}")
            fails += 1
        else:
            # A green check that judged a narrow window is not the same claim
            # as a green check over the whole trace — say which one it was.
            for line in r.stdout.splitlines():
                if "judging" in line or line.startswith("tla_verify --live-trace OK"):
                    print(f"  {line}")
    print(f"selftest: {'FAIL' if fails else 'OK'} ({fails} failures)")
    sys.exit(1 if fails else 0)


# --- zombie tracking ------------------------------------------------------
# A ZOMBIE is a rig resource whose owning loop is gone: agent/dind containers,
# per-cell or per-verify kind clusters, orphan tee loggers, stale heartbeat
# files. They accumulate through force-kills and crashes (2026-07-24: nine
# dead control planes were quietly eating half the host's CPU and depressing
# every measured ceiling). Detection is conservative: anything whose owner
# cannot be established gets a long grace period instead of a reap.


def zombies_cmd(args):
    zs_needed = not args.reap
    if zs_needed:
        zs = zombies.find_zombies()
        if not zs:
            print("no zombies")
        for kind, ident, owner, note in zs:
            print(f"{kind:<10} {ident}  owner={owner}  {note}")
        return
    # SINGLETON + rate limit: a mass respawn fires one detached reap per
    # spawned cell — 29 concurrent docker/kind sweeps racing to delete the
    # same targets is itself host contention. One reaper at a time (atomic
    # mkdir; stale if its pid died), and if any reap finished within
    # ZOMBIE_REAP_COOLDOWN_S the new one just exits.
    cooldown = int(os.environ.get("ZOMBIE_REAP_COOLDOWN_S", 120))
    stamp = common.ORCH / ".zombie-reap.done"
    lock = common.ORCH / ".zombie-reap.lock"
    try:
        if time.time() - stamp.stat().st_mtime < cooldown:
            return
    except OSError:
        pass
    common.ORCH.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            holder = int((lock / "pid").read_text())
            os.kill(holder, 0)
            return                      # a live reaper is already sweeping
        except (OSError, ValueError):
            pass                        # stale lock: dead reaper — take over
    (lock / "pid").write_text(str(os.getpid()))
    try:
        for line in zombies.reap_zombies(zombies.find_zombies()):
            if not args.quiet:
                print(line)
        stamp.touch()
    finally:
        subprocess.run(["rm", "-rf", str(lock)], capture_output=True)


def _holder_of(lock_dir):
    """cid named in a mutex's holder file, or ''."""
    try:
        return (lock_dir / "holder").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


def _slot_holders():
    d = common.ORCH / "work-slots"
    return {c for c in (_holder_of(p) for p in sorted(d.glob("slot-*"))) if c}


def _verify_holder():
    return _holder_of(common.ORCH / "verify-lock")


def _loop_of_phase(phase, slot_held):
    """Heartbeat phase -> the model's loop state.

    `idle` is the window between Spawn and AcquireSlot, and AcquireSlot is
    enabled only while the slot is NOT held — so a cell seeded `idle` that
    already holds its slot can never legally reach `agent`, and every verify
    it goes on to run replays as illegal. A slot in hand means AcquireSlot
    has happened: the cell is `agent`, whatever phase it is waiting in.
    """
    if phase == "verify":
        return "verify"
    if not slot_held:
        return "idle"
    return "agent"


def _observed_epoch():
    """Per-cell state as it is RIGHT NOW, in .tla/Runs.tla's own vocabulary.

    outcome  none | green | failed | revoked      (the ledger's verdict)
    intent   run  | paused | killed               (.paused / .cancelled)
    loop     none | idle | agent | verify         (live loop + heartbeat phase)
    attempts 0..Budget                            (judged attempts)
    slot     true | false                         (holds a work slot)
    verify   true | false                         (holds the verify lock)

    Every variable the replay needs, or a cell caught mid-flight by a reset
    is seeded into a state it cannot legally leave.
    """
    loops, boxes = state.loop_parents(), state.containers()
    slot_holders = _slot_holders()
    verify_holder = _verify_holder()
    out = []
    for d in sorted(common.WS.iterdir()):
        if not d.is_dir() or not parse_cell_id(d.name):
            continue
        L = ledger.parse(d)
        outcome = L["verdict"] or "none"
        if (d / ".cancelled").exists():
            intent = "killed"
        elif state.pause_lock(d.name):
            intent = "paused"
        else:
            intent = "run"
        slot = d.name in slot_holders
        if d.name in loops:
            hb = state.heartbeat(d)
            loop = _loop_of_phase((hb or {}).get("phase") or "", slot)
        else:
            loop = "none"
        out.append(dict(cid=d.name, outcome=outcome, intent=intent,
                        loop=loop, attempts=len(L["iters"]),
                        slot=slot, verify=d.name == verify_holder))
    return out


def _epoch_fields(c):
    return (f"outcome={c['outcome']} intent={c['intent']} loop={c['loop']} "
            f"attempts={c['attempts']} slot={str(c['slot']).lower()} "
            f"verify={str(c['verify']).lower()}")


def trace_reset(args):
    """Archive transitions.log and start a new one from a RECORDED state.

    The live-trace check replays this log against .tla/Runs.tla from the
    spec's Init — every cell idle, intent "run", no outcome. That is only true
    of a genuinely cold fleet. Resetting the log at any other moment makes the
    replay judge real events against a starting state that never existed: on
    2026-07-30 the log was reset while 65 cells sat paused, and the `resume
    all` that followed produced 65 "Resume not ENABLED" violations plus 142
    cascaded from them, because Resume requires intent="paused" and the replay
    believed every cell was "run".

    The fix is NOT to have the replayer infer the starting state at read time.
    Inference would silently absorb exactly the mismatches this check exists
    to find — a log missing a Pause that must have happened would become
    indistinguishable from a log that never needed one. Instead the state is
    RECORDED here, once, at a moment the operator chose, as ordinary EPOCH
    lines a human can read and diff against the workspaces:

        <ts>  EPOCH  <cid>  outcome=green intent=paused loop=none attempts=1

    One line per cell that differs from Init; cells already matching Init are
    omitted. Everything after the epoch is checked at full strength. What is
    NOT checked is history from before it — that history was never logged, and
    saying so plainly beats pretending otherwise.
    """
    common.ORCH.mkdir(parents=True, exist_ok=True)
    cells = _observed_epoch()
    default = dict(outcome="none", intent="run", loop="none", attempts=0,
                   slot=False, verify=False)
    differing = [c for c in cells
                 if any(c[k] != v for k, v in default.items())]

    if args.dry_run:
        print(f"would archive {common.TRANSITIONS_LOG.name} and write "
              f"{len(differing)} EPOCH line(s) for {len(cells)} cell(s):")
        for c in differing[:10]:
            print(f"  {c['cid']}  {_epoch_fields(c)}")
        if len(differing) > 10:
            print(f"  ... and {len(differing) - 10} more")
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if common.TRANSITIONS_LOG.exists():
        archived = common.ORCH / f"transitions.archived-{stamp}.log"
        common.TRANSITIONS_LOG.rename(archived)
        print(f"archived {archived.name}")

    ts = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    with common.TRANSITIONS_LOG.open("w") as f:
        f.write(f"# transitions log RESET {ts}\n")
        f.write(f"# EPOCH lines below record the observed state of {len(cells)} "
                f"cell(s) at reset time.\n")
        f.write("# Replay starts from THIS state, not the spec's Init. Cells "
                "matching Init are omitted.\n")
        for c in differing:
            f.write(f"{ts}\tEPOCH\t{c['cid']}\t{_epoch_fields(c)}\n")
    print(f"wrote {len(differing)} EPOCH line(s) ({len(cells) - len(differing)} "
          f"cell(s) already at Init)")
    live = [c for c in differing if c["loop"] != "none"]
    if live:
        print(f"NOTE: {len(live)} cell(s) have a LIVE loop — their in-flight "
              f"attempt straddles the reset:")
        for c in live:
            print(f"  {c['cid']} (loop={c['loop']})")


def _authorable_error(arm):
    from fae.cell.surface import authorable
    try:
        authorable(arm)
    except RuntimeError as e:
        return str(e)
    return ""


def infra(args):
    """Can this host carry each arm? Every arm's infra preflight
    (<Variant>.infra_ok — the check a cell makes before every attempt)
    and its verify image (built when missing), then a sweep of stale
    infra. Nothing per cell is created here. Exit 1 if any arm is
    refused."""
    bad = _probe_arms()
    sys.path.insert(0, str(ROOT))
    from fae.cell import variants as _tr
    for cls in _tr.registry().values():
        cls.sweep()
    if bad:
        sys.exit(f"infra: {bad} arm(s) refused — see hooks.log lines above")


def _probe_arms(arms=None):
    """Every arm's own preflight (its infra_ok: the daemon, the tools)
    and the image its cells are verified in, built here when missing —
    printed one per line; the count refused."""
    sys.path.insert(0, str(ROOT))
    from fae.cell import variants as _tr
    bad = 0
    for arm in sorted(arms or _tr.registry()):
        cell = _tr._ShimCell(f"infra-probe-{arm}", "/nonexistent", ROOT)
        cell.treatment = arm
        variant = _tr.for_cell(cell)
        ok, note = True, ""
        if not _tr.liveness_declared(type(variant)):
            ok, note = False, f"{type(variant).__name__} declares no infra_alive probe"
        elif (undeclared := _authorable_error(arm)):
            ok, note = False, undeclared
        elif not variant.infra_ok():
            ok = False
        if ok:
            try:
                note = variant.image()
            except RuntimeError as e:
                ok, note = False, f"verify image: {str(e).splitlines()[0]}"
        print(f"  [{'ok' if ok else 'HALT'}] {arm}  {note}")
        bad += not ok
    return bad


SMOKE_WORKSPACES = ROOT / "ws-test.nosync"


def _smoke_classify(ws):
    """(green, one-line verdict): the ledger decides, as for any cell;
    metrics.json only localizes the break (the stage, the log to read) when
    the verifier's metrics carry the fields read here."""
    L = common.ledger.parse(ws) if (ws / "iterations.log").is_file() else {}
    try:
        m = json.loads((ws / "metrics.json").read_text())
    except (OSError, ValueError):
        m = None
    e2e = f"{(m or {}).get('e2e_pass')}/{(m or {}).get('e2e_total')}"
    # no ledger verdict: the verifier's own green, when its metrics carry one
    green = L.get("verdict") == "green" if L.get("verdict") else \
        bool(m and m.get("e2e_green") and m.get("scaling_ok") is not False)
    if green:
        if m and "k6_available" in m and not m["k6_available"]:
            return True, (f"GREEN e2e {e2e} — but K6 MISSING, load + scaling gate "
                          f"not measured (install k6)")
        return True, (f"GREEN at attempt {L['green_at']}" if L.get("green_at")
                      else f"GREEN e2e {e2e}")
    if L.get("halt_cause"):
        return False, f"HALT — {L['halt_cause']}. See {ws}/verifier.log"
    if m is None:
        return False, "NO METRICS — the verify never wrote metrics.json (harness break)"
    if m.get("e2e_green") and m.get("scaling_ok") is False:
        return False, (f"SCALING FAILED — e2e {e2e} passed but the policy did not "
                       f"scale ({m.get('scaling_why') or ''}). See {ws}/verify.log")
    stage = m.get("stage_failed") or ""
    where = {"deploy": f"DEPLOY FAILED — deploy.sh non-zero. See {ws}/deploy.log",
             "nostart": f"NEVER STARTED — infra/readiness. See {ws}/deploy.log",
             "e2e": f"E2E FAILED ({e2e}). See {ws}/verify.log",
             "k6": f"K6 STAGE ISSUE. See {ws}/k6.log"}
    return False, where.get(stage, f"NOT GREEN (stage={stage or '?'}). See {ws}/verify.log")


def _reference_cell(cid, arm, rep, workspaces):
    """A Cell over a REFERENCE workspace (the arm's seed plus its reference
    overlay, no agent), constructed only: prepare() seeds it."""
    from fae.cell import Cell
    c = Cell(cid, workspaces=workspaces, root=ROOT)
    for key, value in (("TASK", "T1"), ("TREATMENT", arm),
                       ("CONDITION", "reference"), ("REPEAT", str(rep))):
        c._env.setdefault(key, value)
    return c


def smoke(args):
    """Pipeline check, NOT a scored run: one REFERENCE cell per arm through
    the driver's own entrypoint — prepare fresh (reference overlay seeded),
    then `python3 -m fae.cell T1 <arm> reference <rep> --stub <empty>`:
    no agent, one attempt, the gate. Exercises the bring-ups, the contract,
    the probes and the sampler exactly as a scored cell would.

    Cells are tagged ref_high_smoke_* and live in ws-test.nosync, so nothing
    under the scored tree is touched. One canonical arrangement per arm by
    default (a pipeline check); --full-gate runs all six. Exit 0 iff every
    arm is green; the per-arm verdict names the log to read.
    """
    arms = [a for a in (args.arms.split(",") if args.arms else common.definition().arms)
            if not args.only or args.only in a]
    if not arms:
        sys.exit(f"smoke: no arm matches --only {args.only!r}")
    print("=== SMOKE MODE: model=ref — pipeline check, NOT a scored run "
          f"(cells tagged ref_high_smoke_*, in {SMOKE_WORKSPACES.name}) ===")
    # each arm's own preflight, so a missing daemon, tool or image is named
    # before any infra is spent
    if _probe_arms(arms):
        sys.exit("SMOKE ABORTED: an arm refused this host — see hooks.log lines above")
    env = dict(os.environ, WORKSPACES_DIR=str(SMOKE_WORKSPACES), MODEL="ref",
               SMOKE="1", CONDITION="reference",
               PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    env.setdefault("EFFORT", "high")
    if not args.full_gate:
        # gate_shapes reads SHAPE_GATE off the loaded config's raw values
        # (fae/cell/config.py seeds it from this env var); anything but
        # "all" is the single canonical arrangement.
        env["SHAPE_GATE"] = "one"
    empty = Path(tempfile.mkdtemp(prefix="stub-empty-"))
    results = []
    for arm in arms:
        cid = cell_id("ref", arm, "reference", args.rep, "T1",
                      effort=env["EFFORT"], smoke=True)
        print(f"\n=== CELL {cid} — prepare -> verify", flush=True)
        t0 = time.time()
        try:
            verbs = common.definition().verbs
            c = (verbs["reference_cell"](cid, arm, args.rep, SMOKE_WORKSPACES)
                 if "reference_cell" in verbs else
                 _reference_cell(cid, arm, args.rep, SMOKE_WORKSPACES))
            c.prepare(fresh=True)
        except (OSError, RuntimeError, FileNotFoundError) as e:
            print(f"    VERDICT: PREPARE FAILED — {e}")
            results.append((arm, False, f"PREPARE FAILED — {e}"))
            continue
        print(f"  prepared: {c.ws}", flush=True)
        p = subprocess.run(ops._cell_argv("T1", arm, "reference", args.rep)
                           + ["--stub", str(empty)], cwd=str(ROOT), env=env)
        green, verdict = _smoke_classify(c.ws)
        green = green and p.returncode == 0
        if p.returncode != 0:
            verdict += f" [driver rc={p.returncode}]"
        print(f"    VERDICT: {verdict}  ({time.time() - t0:.0f}s)", flush=True)
        results.append((arm, green, verdict))
    print("\n=== SMOKE SUMMARY ===")
    for arm, green, verdict in results:
        print(f"  {'ok  ' if green else 'FAIL'} {arm:14s} {verdict}")
    if all(g for _, g, _ in results):
        print("  PIPELINE OK on every arm.")
        sys.exit(0)
    print("  PIPELINE BROKE — see the per-arm VERDICT above; each names the "
          "log to read.", file=sys.stderr)
    sys.exit(1)


def init(args):
    """Write fae.toml at the root with every key at its default (the engine's
    keys, the caps for the locks the variants declare, the machine-local keys
    the experiment declares), then the operator edits it. Refuses to overwrite
    an existing one."""
    from fae.cell import config as _config
    target = ROOT / _config.TOML
    experiment = getattr(args, "experiment", None) or None
    if target.exists():
        sys.exit(f"init: {target} exists — set [paths] experiment_dir there (or "
                 f"EXPERIMENT_DIR in the environment), or move it aside first")
    if experiment:
        # the file is rendered for THAT experiment: its locks, its CONFIG keys
        os.environ["EXPERIMENT_DIR"] = experiment
    target.write_text(_config.render_default_toml(common.definition(), experiment))
    print(f"wrote {target} — edit [paths] if the siblings are elsewhere")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("next: `python3 cli.py experiment check --walk`, step by step through "
              "what the experiment needs before a cell runs")
        return
    if input("Walk through the readiness check now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        from fae.driver import check
        check.main(SimpleNamespace(walk=True, static=False, smoke=False, arms="", task="T1"))


def prepare(args):
    """Seed the whole matrix's workspaces for MODEL (--reps reps) and launch
    nothing — fae/cell/prepare.py's prepare(), the same call the driver
    makes at every start. FRESH=1 in the environment moves an existing
    workspace aside first (safe_wipe; never a delete)."""
    model = args.model or os.environ.get("MODEL")
    if not model:
        sys.exit("prepare: --model MODEL (or MODEL in the environment) is required")
    sys.path.insert(0, str(ROOT))
    from fae.cell import prepare as _prepare
    from fae.cell import config as _config
    from fae.cell import Cell as _Cell
    cfg = _config.load(ROOT)
    n = 0
    for rep in range(1, args.reps + 1):
        for treatment, conditions in common.definition().matrix.items():
            for condition in conditions:
                cid = cell_id(model, treatment, condition, rep, args.task)
                ws = _prepare.prepare(cid, args.task, treatment, condition, rep,
                                      workspaces=common.WS, root=ROOT,
                                      fresh=bool(os.environ.get("FRESH")),
                                      impl=_Cell.IMPL,
                                      model_version=cfg.get("AGENT_MODEL") or model,
                                      cfg=cfg)
                print(f"  prepared {ws}")
                n += 1
    print(f"Prepared matrix: {n} workspace(s).")


def verb_cmd(name, argv):
    """`experiment verb`: one of the experiment's own commands (its
    definition's commands()), `argv` passed through; no name lists them."""
    try:
        commands = common.definition().commands
    except FileNotFoundError as e:
        sys.exit(f"verb: {e} — `python3 cli.py experiment check` names what is missing")
    if not name:
        if not commands:
            print("this experiment defines no commands (commands() in its definition)")
        for n, fn in sorted(commands.items()):
            doc = (fn.__doc__ or "").strip().splitlines()
            print(f"  {n:16s} {doc[0] if doc else ''}")
        return 0
    if name not in commands:
        sys.exit(f"verb: {name!r} is not a command of this experiment "
                 f"({', '.join(sorted(commands)) or 'it defines none'})")
    return commands[name](list(argv))
