"""fae/driver/zombies.py: the small process/docker-probe helpers, and the two
functions that matter most and had the least coverage — find_zombies()
(what's orphaned) and reap_zombies() (what to do about it). test_cluster_
name.py owns _cksum/_cluster_for; test_reconcile_safety.py owns
_leaked_lock_holders/_VERIFY_HOLDER_ARGV. This file is everything else:
the OS-facing helpers and the zombie classes find_zombies enumerates.
"""
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _ctx import runs

zombies = runs.zombies
common = runs.common
state = runs.state
mutex = runs.mutex


class TestPidAlive(unittest.TestCase):
    def test_a_running_pid_is_alive(self):
        with mock.patch.object(zombies.os, "kill") as k:
            self.assertTrue(zombies._pid_alive(os.getpid()))
        k.assert_called_once_with(os.getpid(), 0)

    def test_a_gone_pid_is_not_alive(self):
        with mock.patch.object(zombies.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(zombies._pid_alive(99999))

    def test_a_pid_this_process_cannot_signal_is_alive(self):
        with mock.patch.object(zombies.os, "kill", side_effect=PermissionError):
            self.assertTrue(zombies._pid_alive(1))

    def test_a_non_numeric_pid_is_not_alive(self):
        self.assertFalse(zombies._pid_alive("not-a-pid"))
        self.assertFalse(zombies._pid_alive(None))


class TestDockerAgeS(unittest.TestCase):
    def test_a_valid_timestamp_is_a_positive_age(self):
        past = zombies.datetime.now(zombies.timezone.utc)
        with mock.patch.object(common, "sh", return_value=past.isoformat()):
            age = zombies._docker_age_s("fae-dind-x")
        self.assertGreaterEqual(age, 0)

    def test_an_unparseable_timestamp_is_none(self):
        with mock.patch.object(common, "sh", return_value="garbage"):
            self.assertIsNone(zombies._docker_age_s("fae-dind-x"))


class TestIterAgeS(unittest.TestCase):
    def test_an_existing_log_has_an_age(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            with mock.patch.object(common, "WS", ws):
                (ws / "cid1").mkdir()
                (ws / "cid1" / "iterations.log").write_text("x")
                self.assertIsNotNone(zombies._iter_age_s("cid1"))

    def test_a_missing_log_is_none(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(common, "WS", Path(d)):
            self.assertIsNone(zombies._iter_age_s("no-such-cid"))


class TestFdHolders(unittest.TestCase):
    def test_digit_tokens_become_pids(self):
        with mock.patch.object(common, "sh", return_value="111\n222\n"):
            self.assertEqual(zombies._fd_holders(Path("/x")), [111, 222])

    def test_no_holders_is_empty(self):
        with mock.patch.object(common, "sh", return_value=""):
            self.assertEqual(zombies._fd_holders(Path("/x")), [])


class TestContainersAll(unittest.TestCase):
    def test_docker_ps_dash_a_names_become_a_set(self):
        with mock.patch.object(common, "sh", return_value="fae-agent-x\nfae-dind-x\n"):
            self.assertEqual(zombies._containers_all(),
                             {"fae-agent-x", "fae-dind-x"})


class TestKillProcess(unittest.TestCase):
    def test_dies_on_term_no_kill_sent(self):
        with mock.patch.object(zombies.os, "kill") as k, \
                mock.patch.object(zombies, "_pid_alive", return_value=False), \
                mock.patch.object(zombies.time, "sleep"):
            zombies._kill_process(123)
        k.assert_called_once_with(123, zombies.signal.SIGTERM)

    def test_survives_term_gets_killed(self):
        with mock.patch.object(zombies.os, "kill") as k, \
                mock.patch.object(zombies, "_pid_alive", return_value=True), \
                mock.patch.object(zombies.time, "sleep"):
            zombies._kill_process(123)
        self.assertEqual(k.call_args_list,
                         [mock.call(123, zombies.signal.SIGTERM)] * 1 +
                         [mock.call(123, zombies.signal.SIGKILL)])

    def test_already_gone_is_quiet(self):
        with mock.patch.object(zombies.os, "kill", side_effect=OSError):
            zombies._kill_process(123)   # must not raise


class TestJanitorLines(unittest.TestCase):
    def test_an_old_entry_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            tbd = ws / ".to_be_deleted"
            old = tbd / "20260101-000000" / "some-cid"
            old.mkdir(parents=True)
            old_time = time.time() - 30 * 3600
            os.utime(old.parent, (old_time, old_time))
            with mock.patch.object(common, "WS", ws), \
                    mock.patch.object(common, "SMOKE_WS", ws / "smoke"), \
                    mock.patch.object(common, "ROOT", ws):
                lines = zombies.janitor_lines()
            self.assertEqual(len(lines), 1)
            self.assertIn("review + confirm", lines[0])

    def test_a_young_entry_is_not_reported(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / ".to_be_deleted" / "20260101-000000").mkdir(parents=True)
            with mock.patch.object(common, "WS", ws), \
                    mock.patch.object(common, "SMOKE_WS", ws / "smoke"), \
                    mock.patch.object(common, "ROOT", ws):
                self.assertEqual(zombies.janitor_lines(), [])

    def test_no_to_be_deleted_dir_is_empty(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(common, "WS", Path(d)), \
                mock.patch.object(common, "SMOKE_WS", Path(d) / "smoke"):
            self.assertEqual(zombies.janitor_lines(), [])


class FindZombiesCase(unittest.TestCase):
    """Common no-op baseline for every find_zombies() input, so each test
    below only overrides the one signal it's testing."""

    def setUp(self):
        self.patches = [
            mock.patch.object(zombies, "_leaked_lock_holders", return_value=[]),
            mock.patch.object(state, "loop_parents", return_value=set()),
            mock.patch.object(state, "containers", return_value=[]),
            mock.patch.object(common, "sh", return_value=""),
            mock.patch.object(state, "loop_pids", return_value={}),
            mock.patch.object(zombies, "_strays", return_value=[]),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        ws = Path(self.tmp.name)
        self.ws_patch = mock.patch.object(common, "WS", ws)
        self.ws_patch.start()
        self.addCleanup(self.ws_patch.stop)
        # no arm-*.slots dirs, no zombie candidates there
        self.orch_patch = mock.patch.object(common, "ORCH", ws / ".orch")
        self.orch_patch.start()
        self.addCleanup(self.orch_patch.stop)
        (ws / ".orch").mkdir()


class TestFindZombiesContainers(FindZombiesCase):
    def test_an_old_unowned_container_is_a_zombie(self):
        with mock.patch.object(state, "containers", return_value=["fae-agent-cid1"]), \
                mock.patch.object(zombies, "_iter_age_s", return_value=None), \
                mock.patch.object(zombies, "_docker_age_s",
                                  return_value=zombies.ZOMBIE_UNKNOWN_GRACE_S + 1):
            zs = zombies.find_zombies()
        self.assertIn(("container", "fae-agent-cid1", "cid1", "no live loop"), zs)

    def test_a_live_owned_container_is_not_a_zombie(self):
        with mock.patch.object(state, "loop_parents", return_value={"cid1"}), \
                mock.patch.object(state, "containers", return_value=["fae-agent-cid1"]):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])

    def test_a_too_young_container_is_not_a_zombie(self):
        with mock.patch.object(state, "containers", return_value=["fae-dind-cid1"]), \
                mock.patch.object(zombies, "_iter_age_s", return_value=1.0):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])


class TestFindZombiesClusters(FindZombiesCase):
    def test_an_old_unmapped_cluster_is_a_zombie(self):
        with mock.patch.object(common, "sh", return_value="fx-cluster-12345\n"), \
                mock.patch.object(zombies, "_docker_age_s",
                                  return_value=zombies.ZOMBIE_UNKNOWN_GRACE_S + 1):
            zs = zombies.find_zombies()
        self.assertIn(("cluster", "fx-cluster-12345", "?", "no live owner"), zs)

    def test_an_age_unknown_cluster_is_never_reaped(self):
        with mock.patch.object(common, "sh", return_value="fx-cluster-12345\n"), \
                mock.patch.object(zombies, "_docker_age_s", return_value=None):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])


class TestFindZombiesTee(FindZombiesCase):
    def test_an_orphaned_tee_ppid_1_is_a_zombie(self):
        with mock.patch.object(state, "loop_pids", return_value={321: "cid1"}), \
                mock.patch.object(common, "sh", return_value="1\n"):
            zs = zombies.find_zombies()
        self.assertIn(("tee", "321", "cid1", "orphaned logger (ppid 1)"), zs)

    def test_a_tee_with_a_real_parent_is_not_a_zombie(self):
        with mock.patch.object(state, "loop_pids", return_value={321: "cid1"}), \
                mock.patch.object(common, "sh", return_value="500\n"):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])


class TestFindZombiesHeartbeat(FindZombiesCase):
    def test_a_dead_loop_file_is_a_corpse(self):
        ws = common.WS
        cell = ws / "cid1"
        cell.mkdir()
        (cell / ".loop").write_text("x")
        with mock.patch.object(state, "heartbeat", return_value=None):
            zs = zombies.find_zombies()
        self.assertIn(("heartbeat", str(cell / ".loop"), "cid1", "corpse file"), zs)

    def test_a_beating_loop_file_is_not_a_corpse(self):
        ws = common.WS
        cell = ws / "cid1"
        cell.mkdir()
        (cell / ".loop").write_text("x")
        with mock.patch.object(state, "heartbeat", return_value=1.0):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])


class TestFindZombiesWithoutAWorkspaceRoot(FindZombiesCase):
    def test_a_missing_workspace_root_has_no_zombies(self):
        with mock.patch.object(common, "WS", common.WS / "absent"):
            self.assertEqual(zombies.find_zombies(), [])


class TestFindZombiesLockholders(FindZombiesCase):
    def test_a_leaked_lock_is_reported_with_its_holder_name(self):
        with mock.patch.object(zombies, "_leaked_lock_holders",
                               return_value=[("rig-lock", 777)]), \
                mock.patch.object(mutex, "holder_name", return_value="sonnet_x"):
            zs = zombies.find_zombies()
        self.assertIn(("lockholder", "777:rig-lock", "sonnet_x",
                       "holds rig-lock with no process entitled to it"), zs)


class TestFindZombiesStrays(FindZombiesCase):
    def test_an_old_stray_process_is_a_zombie(self):
        with mock.patch.object(zombies, "_strays",
                               return_value=[("process", "555:cid1", "cid1")]), \
                mock.patch.object(zombies, "_iter_age_s",
                                  return_value=zombies.ZOMBIE_UNKNOWN_GRACE_S + 1):
            zs = zombies.find_zombies()
        self.assertIn(("process", "555:cid1", "cid1", "no live loop"), zs)

    def test_a_fresh_stray_process_is_not_yet_a_zombie(self):
        with mock.patch.object(zombies, "_strays",
                               return_value=[("process", "555:cid1", "cid1")]), \
                mock.patch.object(zombies, "_iter_age_s", return_value=1.0):
            zs = zombies.find_zombies()
        self.assertEqual(zs, [])


class TestReapZombies(unittest.TestCase):
    def test_container_is_docker_rm_dash_f_v(self):
        with mock.patch.object(subprocess, "run") as r:
            done = zombies.reap_zombies([("container", "fae-agent-x", "cid1", "note")])
        r.assert_called_once_with(["docker", "rm", "-f", "-v", "fae-agent-x"],
                                  capture_output=True, timeout=60)
        self.assertIn("reaped container fae-agent-x (cid1)", done)

    def test_cluster_is_kind_delete(self):
        with mock.patch.object(subprocess, "run") as r:
            zombies.reap_zombies([("cluster", "exp-ka-1", "cid1", "note")])
        r.assert_called_once_with(["kind", "delete", "cluster", "--name", "exp-ka-1"],
                                  capture_output=True, timeout=180)

    def test_tee_is_sigterm(self):
        with mock.patch.object(zombies.os, "kill") as k:
            zombies.reap_zombies([("tee", "999", "cid1", "note")])
        k.assert_called_once_with(999, zombies.signal.SIGTERM)

    def test_a_still_dead_heartbeat_is_unlinked(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cid1" / ".loop"
            p.parent.mkdir()
            p.write_text("x")
            with mock.patch.object(state, "heartbeat", return_value=None):
                done = zombies.reap_zombies([("heartbeat", str(p), "cid1", "note")])
            self.assertFalse(p.exists())
            self.assertIn(f"reaped heartbeat {p} (cid1)", done)

    def test_a_heartbeat_beating_again_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "cid1" / ".loop"
            p.parent.mkdir()
            p.write_text("x")
            with mock.patch.object(state, "heartbeat", return_value=1.0):
                done = zombies.reap_zombies([("heartbeat", str(p), "cid1", "note")])
            self.assertTrue(p.exists())
            self.assertIn(f"skipped heartbeat {p} — beating again", done)

    def test_a_still_stray_process_is_killed(self):
        with mock.patch.object(zombies, "_strays",
                               return_value=[("process", "555:cid1", "cid1")]), \
                mock.patch.object(state, "loop_parents", return_value=set()), \
                mock.patch.object(zombies, "_kill_process") as kp:
            done = zombies.reap_zombies([("process", "555:cid1", "cid1", "note")])
        kp.assert_called_once_with(555)
        self.assertIn("reaped process 555:cid1 (cid1)", done)

    def test_a_process_alive_again_is_skipped(self):
        with mock.patch.object(zombies, "_strays", return_value=[]), \
                mock.patch.object(state, "loop_parents", return_value=set()), \
                mock.patch.object(zombies, "_kill_process") as kp:
            done = zombies.reap_zombies([("process", "555:cid1", "cid1", "note")])
        kp.assert_not_called()
        self.assertIn("skipped process 555:cid1 — live again or pid recycled", done)

    def test_a_still_leaked_lockholder_is_killed(self):
        with mock.patch.object(zombies, "_leaked_lock_holders",
                               return_value=[("rig-lock", 777)]), \
                mock.patch.object(zombies, "_kill_process") as kp:
            done = zombies.reap_zombies([("lockholder", "777:rig-lock", "?", "note")])
        kp.assert_called_once_with(777)
        self.assertIn("reaped lockholder 777:rig-lock (?)", done)

    def test_a_lockholder_entitled_again_is_skipped(self):
        with mock.patch.object(zombies, "_leaked_lock_holders", return_value=[]), \
                mock.patch.object(zombies, "_kill_process") as kp:
            done = zombies.reap_zombies([("lockholder", "777:rig-lock", "?", "note")])
        kp.assert_not_called()
        self.assertIn("skipped lockholder 777:rig-lock — entitled again", done)

    def test_a_failure_is_reported_not_raised(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
            done = zombies.reap_zombies([("container", "x", "cid1", "note")])
        self.assertIn("FAILED reaping container x: boom", done)


if __name__ == "__main__":
    unittest.main()


class TestClusterMapAsksTheVariantForTheTech(unittest.TestCase):
    """An arm whose name does not start with its tech (`kedap_access` of
    tech `keda`) still owns the cluster its variant names."""

    def test_the_tech_is_the_variants_not_the_arm_prefix(self):
        from fae.cell import variants as _tr
        beta = _tr.registry()["beta"]
        cid = "testpy_high_beta_apidocs_T1_r1"
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / cid).mkdir()
            with mock.patch.object(common, "WS", ws), \
                    mock.patch.object(beta, "TECH", "shared"), \
                    mock.patch.object(beta, "substrate_identities",
                                      classmethod(lambda cls, c: [("cluster", f"cl-{c}")])), \
                    mock.patch.dict(zombies._CLMAP, {"key": None, "map": {}}):
                self.assertEqual(zombies._cluster_map(), {cid: f"cl-{cid}"})
