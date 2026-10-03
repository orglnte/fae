"""One process per cell run.

Every measurement instrument in this rig is already Python, and so is the cell
that drives them, so spawning `python3 instruments/<x>.py` paid for an
interpreter start and a text round-trip to reach code one import away. It also
made a cell's process tree a moving target: nine different children, each with
an exit code to interpret and its own way to fail.

The rule these tests pin: the cell package starts NO Python child processes.
What it may still start is a program that genuinely is one — k6 is a Go binary,
docker and kubectl are clients, and the bring-up is shell by design.

The instruments are NOT modified for this. They keep main() and their __main__
guard because they are also the operator's command-line tools
(csv_trace_to_pdf); the adapter (verify.call) patches sys.argv and the
environment the way an interpreter start would, and restores both afterwards.
The adapter itself lives in verify.py — call() runs an instrument's main(),
run_in_thread() runs one in a thread, _load_instrument() imports it by path.
"""
import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from _ctx import ROOT

ROOT = Path(ROOT)
PKG = ROOT / "fae" / "cell"
QUEUES_SRC = (ROOT / "fae" / "queues.py").read_text()
CONTRIB = ROOT / "fae" / "cell" / "contrib" / "elastic_resource"
ENGINE_INSTRUMENTS = ROOT / "fae" / "cell" / "instruments"


def _inst(name):
    """The file an instrument name resolves to: the engine's own live beside
    verify.py, the load-shape law's in the elastic_resource block."""
    return (ENGINE_INSTRUMENTS if name == "resource_sampler" else CONTRIB) / f"{name}.py"

from fae.cell import verify               # noqa: E402

# Every instrument the cell reaches for, by the name it is called with. ckpt is
# folded into checkpoints.py and restore_fixed into Cell._restore_fixed — they
# no longer go through the instrument loader.
USED = ["load_shape", "k6", "law", "trace", "resource_sampler"]


class TestNoPythonChildProcesses(unittest.TestCase):

    def _no_venv_line(self, l):
        # The cell's OWN venv is infra, not an instrument child: rig.py
        # builds it (sys.executable -m venv) and runs its interpreter to
        # pip-install the SDK and check the imports, the way the shell
        # venv_template did. Those lines name an interpreter legitimately.
        return not any(t in l for t in ("venv", "template", "pybin"))

    def test_the_package_never_spawns_an_interpreter(self):
        # rig.py builds the cell's own venv (an infra step, not an
        # instrument); verify.py names the interpreter only to compose the
        # sidecar's cache-probe command string, which the sidecar spawns.
        for f in sorted(PKG.glob("*.py")):
            if f.name in ("rig.py", "verify.py"):
                continue
            self.assertNotIn("sys.executable", f.read_text(),
                             f"{f.name} starts a Python child process")

    def test_it_never_shells_to_an_instrument(self):
        for f in sorted(PKG.glob("*.py")):
            # Interpreters the package may name that are not harness children:
            # the experiment venv (EXP_VENV_PY) the infra probe imports the
            # SDK through, the scripted testagent's OWN container, the
            # cell's own venv build (rig.py), and the verify container's own
            # interpreter (CHILD_ARGV: the one child, inside the image).
            body = "\n".join(l for l in f.read_text().splitlines()
                             if "EXP_VENV_PY" not in l and "testagent.py" not in l
                             and "CHILD_ARGV = " not in l
                             and self._no_venv_line(l))
            self.assertNotRegex(body, r'"python3?"',
                                f"{f.name} shells to an interpreter")
            self.assertNotRegex(body, r'INSTRUMENTS / "[a-z_]+\.py"',
                                f"{f.name} still names an instrument as a script")

    def test_the_children_that_remain_are_not_python(self):
        # The allowlist IS the claim: anything else appearing here should fail
        # the test and be argued for explicitly.
        allowed = {"bash", "docker", "kubectl", "cp", "k6", "git",
                   "kind", "curl", "ps", "lsof",   # the variants' infra CLIs
                   "caffeinate",                   # macOS idle-sleep assertion
                   "npm"}                          # the agent clients' upstream versions
        found = set()
        for f in sorted(PKG.glob("*.py")):
            for m in re.finditer(r'subprocess\.(?:run|Popen)\(\[\s*"([a-z0-9_]+)"',
                                 f.read_text()):
                found.add(m.group(1))
        self.assertTrue(found <= allowed, f"unexpected child processes: {found - allowed}")

class TestTheInstrumentsStillWorkAsScripts(unittest.TestCase):
    """They are the operator's command-line tools too, and calling them
    in-process must not have cost that."""

    def test_each_one_keeps_a_main_and_a_guard(self):
        import ast
        for name in USED:
            src = (_inst(name)).read_text()
            tree = ast.parse(src)
            self.assertTrue(
                any(isinstance(n, ast.FunctionDef) and n.name == "main"
                    for n in tree.body), f"{name}: no main()")
            self.assertIn('if __name__ == "__main__":', src, f"{name}: no guard")

    def test_none_of_them_does_work_at_import_time(self):
        # Importing must be free of side effects: the loader imports every
        # instrument into the cell's own process.
        import ast
        for name in USED:
            tree = ast.parse((_inst(name)).read_text())
            work = [n for n in tree.body
                    if not isinstance(n, (ast.Import, ast.ImportFrom,
                                          ast.FunctionDef, ast.ClassDef,
                                          ast.Assign, ast.AnnAssign, ast.If,
                                          ast.Expr))]
            self.assertEqual(work, [], f"{name} does work at import time")

    def test_one_still_runs_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, str(CONTRIB / "load_shape.py"), d],
                               capture_output=True, text=True,
                               env=dict(os.environ, VERIFY_SHAPE="BBS"))
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((Path(d) / "schedule.json").is_file())


class TestTheAdapter(unittest.TestCase):
    """The adapter is verify.call / run_in_thread / _load_instrument. Exercised
    against a fixture instrument in a temp dir (verify.INSTRUMENTS is
    monkeypatched) so the mechanics are pinned independently of any real
    instrument's behaviour."""

    # A minimal instrument with both shapes: it reads argv it is handed, prints
    # a line carrying the env canary, and takes flags for the failure paths.
    FIXTURE = (
        "import os, sys\n"
        "def main(argv=None):\n"
        "    argv = list(sys.argv) if argv is None else list(argv)\n"
        "    if '--raise' in argv: raise RuntimeError('boom')\n"
        "    if '--exit3' in argv: sys.exit(3)\n"
        "    if '--fail' in argv: return 2\n"
        "    print('canary=' + os.environ.get('FAE_TEST_CANARY', 'none'))\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    sys.exit(main())\n"
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.d = Path(self._tmp.name)
        (self.d / "probe.py").write_text(self.FIXTURE)
        self._real_inst = verify.INSTRUMENTS
        verify.INSTRUMENTS = self.d
        verify._inst_cache.clear()
        self.addCleanup(verify._inst_cache.clear)
        self.addCleanup(setattr, verify, "INSTRUMENTS", self._real_inst)

    def test_it_loads_an_instrument_once(self):
        a = verify._load_instrument("probe")
        b = verify._load_instrument("probe")
        self.assertIs(a, b, "the import cost is being paid per call")

    def test_a_missing_instrument_is_a_clear_failure(self):
        with self.assertRaises(FileNotFoundError):
            verify._load_instrument("no_such_instrument")

    def test_argv_is_restored_after_a_call(self):
        # A leaked argv would make the NEXT instrument read the previous one's
        # arguments — a bug that would look like a measurement error.
        before = list(sys.argv)
        verify.call("probe", [])
        self.assertEqual(sys.argv, before)

    def test_the_environment_is_restored_after_a_call(self):
        os.environ["FAE_TEST_CANARY"] = "original"
        self.addCleanup(os.environ.pop, "FAE_TEST_CANARY", None)
        verify.call("probe", [], env={"FAE_TEST_CANARY": "patched"})
        self.assertEqual(os.environ["FAE_TEST_CANARY"], "original")

    def test_it_returns_the_instruments_stdout(self):
        os.environ["FAE_TEST_CANARY"] = "seen"
        self.addCleanup(os.environ.pop, "FAE_TEST_CANARY", None)
        rc, out = verify.call("probe", [])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "canary=seen")

    def test_a_failing_instrument_returns_a_code_and_does_not_raise(self):
        # It used to be a non-zero exit from a child. Every best-effort call
        # site must keep its best-effort semantics rather than silently
        # becoming fatal now that it runs inside the cell.
        rc, _ = verify.call("probe", ["--fail"])
        self.assertEqual(rc, 2)

    def test_an_instrument_that_raises_is_contained(self):
        # The containment mechanism itself: an instrument that throws used to
        # be a non-zero exit from a child, and must not now take the cell down
        # with it.
        rc, _ = verify.call("probe", ["--raise"])
        self.assertEqual(rc, 1)

    def test_a_nonzero_exit_is_returned_not_raised(self):
        rc, _ = verify.call("probe", ["--exit3"])
        self.assertEqual(rc, 3)

    def test_a_concurrent_instrument_runs_in_a_thread_not_a_child(self):
        # Only the resource sampler needs concurrency: it measures the running
        # footprint across the same window the load runs in. It is IO-bound
        # (it shells out to docker and waits), so a thread is the right shape
        # and the cell stays one process.
        h = verify.run_in_thread("probe", [])
        self.assertEqual(h.join(30), 0)
        self.assertFalse(h.alive)
        import inspect
        body = inspect.getsource(verify.run_in_thread)
        self.assertIn("threading.Thread", body)
        self.assertNotIn("subprocess", body,
                         "the thread runner starts a child process")


class TestTheProcessCountClaim(unittest.TestCase):
    """The diagram says two long-lived processes for a Python cell: conduct,
    and the cell. Anything that would add a third is a change to that claim."""

    def test_the_verify_is_a_child_in_its_own_session(self):
        # The third process is the verifier, per arrangement: its own
        # session, killed as a group when it returns, no fd inherited.
        body = (PKG / "cell.py").read_text()
        block = body[body.index("    def verify(self"):body.index("    def _persist_verdict")]
        self.assertIn("run_verifier(", block)
        runner = (PKG / "verify.py").read_text()
        runner = runner[runner.index("def run_verifier("):runner.index("def _end(")]
        self.assertIn("start_new_session=True", runner)
        self.assertNotIn("pass_fds", runner)
        self.assertIn("verify_argv(", runner, "the child runs in a container")

    def test_the_rig_lock_is_held_by_the_cell_process(self):
        body = (PKG / "cell.py").read_text()
        block = body[body.index("    def exclusive_acquire(self"):body.index("    def verify(self")]
        self.assertIn("_mutex.open_lock", block)
        self.assertIn("return fh", block)

    def test_nothing_is_handed_the_slots(self):
        # provisioning is native: no child inherits the lock fds at all
        self.assertNotIn("pass_fds=", (PKG / "cell.py").read_text())


if __name__ == "__main__":
    unittest.main()


class TestTheRunLoopTakesItsLocksAndProvisionsItsArm(unittest.TestCase):
    """The equivalence diff runs under FAE_VARIANT_NOOP, so it cannot see
    any of this. Pinned here instead."""

    BODY = (PKG / "cell.py").read_text()

    def block(self):
        b = self.BODY[self.BODY.index("    def run(self, stub_overlay"):]
        return b[:b.index("\n    def ", 10)]

    def test_it_holds_the_loop_lock_for_the_whole_run(self):
        b = self.block()
        self.assertIn("self.loop_lock()", b)
        self.assertIn("loop_lock.close()", b)
        self.assertLess(b.index("loop_lock = self.loop_lock()"),
                        b.index("self.prepare()"))

    def test_a_second_loop_on_one_workspace_is_refused(self):
        # A benign race (LOCK_EXIT), not a crash — must stay a Halt with its
        # own code, or a real driver crash could land on the same exit code
        # and be silently retried forever like a lock refusal would be.
        b = self.block()
        self.assertIn("if loop_lock is None:", b)
        self.assertIn("raise Halt(", b)
        self.assertIn("self.LOCK_EXIT", b)

    def test_the_slots_are_held_across_the_setup_hook(self):
        # Without its slots a cell holds no work slot and no lock slot, and
        # every cap is bypassed: it runs only on the slots handed to it, or
        # when asked explicitly to run without.
        b = self.block()
        self.assertIn("slots = self.queues.adopt_slots(self.cid, handed)", b)
        self.assertLess(b.index("slots = self.queues.adopt_slots(self.cid, handed)"),
                        b.index("self.setup()"))
        self.assertIn("elif not ignore_slots:", b)
        self.assertIn("slots.close()", b)

    def test_setup_and_teardown_both_run(self):
        b = self.block()
        self.assertIn("self.setup()", b)
        self.assertIn("self.teardown()", b)

    def test_teardown_and_release_are_on_the_unconditional_path(self):
        b = self.block()
        tail = b[b.index("finally:"):]
        for step in ("stop_ticker()", "self.teardown()", "slots.close()",
                     "loop_lock.close()"):
            self.assertIn(step, tail, f"{step} is not guaranteed to run")
        self.assertLess(tail.index("self.teardown()"), tail.index("slots.close()"),
                        "the slots are released before teardown uses its locks")

    def test_a_rig_fault_does_not_consume_an_attempt(self):
        # `charge` is the verifier's word; the loop keeps no table of stages
        b = self.block()
        self.assertIn("not last.charge", b)
        self.assertNotIn("VOID_STAGES", self.BODY)

    def test_a_heartbeat_ticker_runs_while_a_phase_blocks(self):
        # Supervision reads phase age; without a ticker a long agent call looks
        # like a dead loop.
        self.assertIn("start_ticker", self.block())

    def test_reverify_provisions_the_arm_too(self):
        b = self.BODY[self.BODY.index("    def reverify(self"):]
        b = b[:b.index("\n    def ", 10)]
        self.assertIn("self.setup()", b)
        self.assertIn("self.teardown()", b)

    def test_only_a_variant_that_declares_a_lock_takes_one(self):
        from fae.cell.cell import Cell
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for variant, lock in (("alpha_apidocs", "alpha"),
                                  ("beta_apidocs", None)):
                c = Cell("x", workspaces=Path(d), root=Path(d))
                c._env["VARIANT"] = variant
                self.assertEqual(c.arm, lock, variant)


class TestBothImplementationsAreSpawnable(unittest.TestCase):

    def test_ops_spawn_can_start_either(self):
        src = (ROOT / "fae" / "driver" / "ops.py").read_text()
        self.assertIn('"-m", "fae.cell"', src)

    def test_the_package_has_an_entry_point(self):
        self.assertTrue((PKG / "__main__.py").is_file())

    def test_the_entry_point_does_not_reimplement_cell_id(self):
        src = (PKG / "__main__.py").read_text()
        self.assertIn("common.cell_id(", src)


class TestTheVerifySurfaceIsFingerprinted(unittest.TestCase):
    """The fingerprint voids an attempt whose arrangements were measured by two
    different verifies. The Python driver is a verify surface, so it is in it."""

    def fp(self):
        from fae.cell import config as cfgmod, rig
        # load_config exports FP_EXTRA_FILES; the config cache is per process,
        # so the file list is read once and the hash is what moves.
        return rig.fp(ROOT, cfgmod.load(ROOT).values)

    def test_every_module_of_the_package_is_covered(self):
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        extras = cfgmod.load(ROOT).values.get("FP_EXTRA_FILES", "").split()
        listed = {Path(f).relative_to(PKG).as_posix() for f in extras if "/fae/cell/" in f}
        self.assertEqual(listed, {f.relative_to(PKG).as_posix() for f in PKG.rglob("*.py")})

    def test_an_experiment_root_without_the_engine_still_guards_the_engine(self):
        # the experiment repo imports the engine and does not contain it: the
        # cell modules are found where the engine is, never under the root
        from fae.cell import config as cfgmod
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        cfgmod._cache.clear()
        self.addCleanup(cfgmod._cache.clear)
        extras = cfgmod.load(root).values.get("FP_EXTRA_FILES", "").split()
        listed = {Path(f).relative_to(PKG).as_posix() for f in extras if "/fae/cell/" in f}
        self.assertEqual(listed, {f.relative_to(PKG).as_posix() for f in PKG.rglob("*.py")})

    def test_editing_the_driver_moves_the_fingerprint(self):
        # On a COPY of the guarded tree: the checkout is what live cells are
        # fingerprinted against, and a probe written there is a fleet-wide void.
        import shutil, tempfile
        from fae.cell import rig
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        shutil.copytree(ROOT / "tests" / "fixture_experiment", root / "experiment",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "fae" / "cell", root / "fae" / "cell")
        cell = root / "fae" / "cell"
        env = {"FP_EXTRA_FILES": " ".join(sorted(str(f) for f in cell.rglob("*.py")))}
        before = rig.fp(root, env)
        target = cell / "checkpoints.py"
        target.write_text(target.read_text() + "\n# fingerprint probe\n")
        self.assertNotEqual(rig.fp(root, env), before)

    def test_the_cell_pins_it_once_not_once_per_arrangement(self):
        # Pinning inside each arrangement would miss a change made between
        # arrangement 3 and 4 — the case the guard exists for.
        body = (PKG / "cell.py").read_text()
        self.assertIn("expected_fp=self.expected_fp", body)
        block = body[body.index("    def expected_fp(self)"):]
        self.assertIn("if self._fp is None:", block[:block.index("\n    def ", 10)])


class TestTheCellTakesItsOwnLocks(unittest.TestCase):
    """The cell process flocks its own fds, so the holder is the process that
    lives for the whole cell. The setup hook then only provisions."""

    BODY = (PKG / "cell.py").read_text()

    def test_the_slot_is_taken_before_the_hook_provisions(self):
        b = self.BODY[self.BODY.index("    def run(self, stub_overlay"):]
        b = b[:b.index("\n    def ", 10)]
        self.assertLess(b.index("self.queues.adopt_slots("), b.index("self.setup()"))

    def test_the_slot_is_held_for_the_CELL_not_per_attempt(self):
        # Releasing between attempts would leave the next one with no slot to
        # verify under, and the model would refuse its AcquireVerify.
        b = self.BODY[self.BODY.index("    def run(self, stub_overlay"):]
        loop = b[b.index("for attempt in range("):b.index("self._append(\"END\"")]
        self.assertNotIn("T.ADMIT", loop)
        # the one release inside the loop is on the rig-fault path, which ends
        # the cell rather than continuing to the next attempt — by returning or
        # by raising Halt, which carries the exit code out to the supervisor
        for i, line in enumerate(loop.splitlines()):
            if "RELEASE_SLOT" in line:
                after = "\n".join(loop.splitlines()[i:i + 3])
                self.assertTrue("return None" in after or "raise Halt" in after,
                                line.strip())

    def test_the_lock_slot_is_taken_after_the_work_slot(self):
        # Without a free work slot no lock slot is touched, and a busy lock
        # pool gives the work slot back: nothing is held while waiting.
        b = QUEUES_SRC[QUEUES_SRC.index("    def try_slots(self"):]
        b = b[:b.index("\n    def ", 10)]
        self.assertIn('("work", "lock") if lock else ("work",)', b)
        self.assertIn("slots.close()\n                    return None, pool", b)

    def test_a_failed_attempt_leaves_the_cell_able_to_verify_again(self):
        from fae.cell.fsm import State, T, step
        s = State()
        step(s, T.ADMIT)
        for _ in range(3):
            step(s, T.ACQUIRE_VERIFY)
            step(s, T.VERIFY_FAIL)
        step(s, T.ACQUIRE_VERIFY)
        step(s, T.VERIFY_GREEN)
        self.assertEqual(s.outcome, "green")


class TestConcurrentInstrumentsShareNoGlobals(unittest.TestCase):
    """Two instruments overlap during the load window (sampler thread +
    sidecar). sys.argv and os.environ are process-global: a per-call
    save/patch/restore interleaved across threads resurrects the other
    context's variables — the leaked KUBECONFIG made a keda deploy skip
    provisioning and void an attempt as nostart."""

    def test_spawn_takes_no_env(self):
        import inspect
        self.assertNotIn("env", inspect.signature(verify.run_in_thread).parameters)

    def test_the_overlapping_instruments_accept_argv(self):
        import importlib.util
        for name in ("resource_sampler", "trace"):
            path = _inst(name)
            spec = importlib.util.spec_from_file_location(f"_t_{name}", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            import inspect
            self.assertIn("argv", inspect.signature(m.main).parameters,
                          f"{name}.main reads the process-global sys.argv")

    def test_an_argv_taking_call_leaves_sys_argv_alone(self):
        src = (Path(ROOT) / "fae" / "cell" / "verify.py").read_text()
        self.assertIn("None if takes_argv else argv", src)
        block = src[src.index("def _as_script"):]
        block = block[:block.index("\ndef ")]
        self.assertIn("if argv is not None:", block)
        self.assertIn("if env:", block)

class TestTheContractStandDownIsAModeledPath(unittest.TestCase):
    """cell.contract_stand_down after a void VerifyFail: Pause then StandDown
    from the state the model leaves the loop in. No new transition."""

    def test_void_then_pause_then_stand_down_is_legal_below_budget(self):
        from fae.cell.fsm import State, T, step
        s = State()
        for t in (T.ADMIT, T.ACQUIRE_VERIFY, T.VERIFY_FAIL, T.PAUSE, T.STAND_DOWN):
            step(s, t)
        self.assertEqual(s.intent, "paused")
        self.assertIsNone(s.outcome)

    def test_the_driver_pauses_the_cell_itself_and_never_halts_43_on_a_contract_void(self):
        src = (Path(ROOT) / "fae" / "cell" / "cell.py").read_text()
        body = src[src.index("def contract_stand_down"):]
        body = body[:body.index("\n    def ")]
        for needle in ('"ALERT"', '".paused"', "note_pause()", 'T.STAND_DOWN, "contract"'):
            self.assertIn(needle, body)
        self.assertNotIn("Halt(", body)
        void = src[src.index("if last is not None and not last.charge and broken:"):
                   src.index("if last is not None and not last.charge:")]
        self.assertIn("contract_stand_down", void)
        self.assertNotIn("Halt(", void)

class TestTheStubFlagIsTheRigDebugPath(unittest.TestCase):

    def test_stub_is_parsed_out_and_handed_to_run(self):
        from fae.cell import __main__ as entry
        seen = {}

        class FakeCell:
            IGNORE_SLOTS_ENV = "CELL_IGNORE_SLOTS"

            def __init__(self, cid):
                self.ws = "/w"

            @classmethod
            def new(cls, cid, task, variant, rep, reference=False):
                return cls(cid)

            def run(self, stub_overlay=None, ignore_slots=False):
                seen["stub"] = stub_overlay
                return "green"
        with mock.patch.object(entry, "Cell", FakeCell), \
             mock.patch.dict(os.environ, {"AGENT": "ref", "SMOKE": "1"}):
            rc = entry.main(["T1", "alpha", "reference", "2", "--stub", "/tmp/empty"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["stub"], "/tmp/empty")
        self.assertEqual(entry.main(["T1", "alpha", "reference", "2", "--stub"]), 2)
