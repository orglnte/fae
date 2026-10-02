"""The engine carries no shell but the agent image's build script.

The driver, the verify, the config and the variants' hooks are Python
(fae/cell); these tests pin that, so nobody re-adds a driver script, a hook,
a config loader or a helper "for convenience".
"""
import unittest
from pathlib import Path

from _ctx import ROOT

HARNESS = Path(ROOT) / "fae"

GONE = [
    "harness/lib.sh", "harness/config.example.sh", "harness/venv_template.sh",
    "harness/cache_probe.sh", "harness/alpha.sh",
    "harness/run_exp_artifact.sh", "harness/smoke.sh", "harness/prepare_cell.sh",
    "harness/ensure_infra.sh", "harness/_transitions.sh",
    "harness/variants/_mutex.sh", "harness/variants/_work_slots.sh",
    "harness/variants/_arm_lock.sh", "harness/variants/_cell_venv.sh",
] + [f"harness/variants/{arm}/{hook}.sh"
     for arm in ("beta", "alpha", "beta", "alpha",
                 "beta", "alpha")
     for hook in ("cell_setup", "cell_teardown", "infra")]

# The bash that remains: the agent image build.
STAYS = ["fae/agent-container/build.sh"]


class TestTheFilesAreGone(unittest.TestCase):

    def test_every_retired_script_is_absent(self):
        present = [f for f in GONE if (Path(ROOT) / f).exists()]
        self.assertEqual(present, [], f"retired bash is back: {present}")

    def test_the_treatments_directory_itself_is_gone(self):
        self.assertFalse((HARNESS / "variants").exists())

    def test_what_stays_stays(self):
        missing = [f for f in STAYS if not (Path(ROOT) / f).is_file()]
        self.assertEqual(missing, [])

    def test_the_only_shell_under_harness_is_the_bringup_and_the_build(self):
        found = {p.relative_to(ROOT).as_posix() for p in HARNESS.rglob("*.sh")
                 if ".agent-home" not in p.parts}
        self.assertTrue(found <= set(STAYS), f"unexpected shell: {found - set(STAYS)}")


class TestConfigAndCacheAreNativeNow(unittest.TestCase):

    def test_the_guarded_extras_name_only_files_that_exist(self):
        from fae.cell import config as cfgmod
        cfgmod._cache.clear()
        extras = cfgmod.load(ROOT).values.get("FP_EXTRA_FILES", "").split()
        self.assertEqual([f for f in extras if not Path(f).is_file()], [])
        names = {Path(f).name for f in extras}
        for kept in ("verify.py", "cell.py", "config.py"):
            self.assertIn(kept, names, kept)
        for gone in ("lib.sh", "venv_template.sh", "prepare_cell.sh", "smoke.sh"):
            self.assertNotIn(gone, names, gone)


class TestTheFingerprintNamesNoRetiredFile(unittest.TestCase):

    def test_rig_fp_hashes_what_exists(self):
        src = (HARNESS / "cell" / "rig.py").read_text()
        body = src[src.index("def fp("):]
        body = body[:body.index("\ndef ")]
        for kept in ('"EXPERIMENT_DIR"', '"FP_EXTRA_FILES"'):
            self.assertIn(kept, body, kept)
        for gone in ('"lib.sh"', '"cache_probe.sh"', "run_exp_artifact"):
            self.assertNotIn(gone, body, gone)

    def test_the_cell_package_names_no_retired_file(self):
        for f in sorted((HARNESS / "cell").glob("*.py")):
            for line in f.read_text().splitlines():
                if "retired" in line or "ported" in line:
                    continue
                for name in ("run_exp_artifact.sh", "smoke.sh", "_mutex.sh",
                             "prepare_cell.sh", "_cell_venv.sh", "cell_setup.sh",
                             "_transitions.sh", "cache_probe.sh"):
                    self.assertNotIn(name, line, f"{f.name} still names {name}: {line}")


if __name__ == "__main__":
    unittest.main()
