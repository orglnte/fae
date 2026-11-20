"""Experiments reach FAE only through its public SDK ports: `fae.experiment`,
`fae.cell.verify`, `fae.cell.substrate`, `fae.cell.infra.base` (public symbols),
`fae.cell.verify_host`, `fae.cell.contrib`, and `fae.cli`.

Direct imports of engine internals (`fae.cell.cell`, `fae.cell.host_sleep`,
`fae.cell.image`, `fae.cell.infra.{dind,kind,secrunner}`, `fae.conduct`),
private FAE helpers (`_run`, `_ok`, `_now`, `_ShimCell`), or reflection on
`INFRA.__module__` are boundary violations.

`boundary_offenders(root)` is the scan; an experiment's own suite can import
and run it on its tree.
"""
import ast
import tempfile
import unittest
from pathlib import Path

from _ctx import _EXPERIMENT

FORBIDDEN_MODULE_PREFIXES = (
    "fae.cell.cell",
    "fae.cell.host_sleep",
    "fae.cell.image",
    "fae.cell.infra.dind",
    "fae.cell.infra.kind",
    "fae.cell.infra.secrunner",
    "fae.conduct",
)

FORBIDDEN_IMPORTED_NAMES = {
    "_ShimCell",
    "_now",
    "_run",
    "_ok",
}

FORBIDDEN_PRIVATE_ATTRS = {
    "_now",
    "_run",
    "_ok",
}


def _matches_forbidden_module(name):
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN_MODULE_PREFIXES)


def _fae_imported_aliases(tree):
    """Local names bound to `fae` or any `fae.*` module in `tree`."""
    aliases = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "fae" or a.name.startswith("fae."):
                    aliases.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod == "fae" or mod.startswith("fae."):
                for a in n.names:
                    aliases.add(a.asname or a.name)
    return aliases


def boundary_offenders(root):
    """[(file, line, what)] of every FAE SDK boundary violation under `root`
    (ignoring `tests/` and `__pycache__/`)."""
    out = []
    for p in sorted(Path(root).rglob("*.py")):
        if "__pycache__" in p.parts or "tests" in p.parts:
            continue
        tree = ast.parse(p.read_text())
        fae_names = _fae_imported_aliases(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if _matches_forbidden_module(a.name):
                        out.append((str(p), n.lineno, f"import {a.name}"))
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                if _matches_forbidden_module(mod):
                    out.append((str(p), n.lineno, f"from {mod}"))
                elif mod == "fae" or mod.startswith("fae."):
                    for a in n.names:
                        full = f"{mod}.{a.name}"
                        if _matches_forbidden_module(full):
                            out.append((str(p), n.lineno, f"from {mod} import {a.name}"))
                        elif a.name in FORBIDDEN_IMPORTED_NAMES:
                            out.append((str(p), n.lineno, f"from {mod} import {a.name}"))
            elif isinstance(n, ast.Attribute):
                if n.attr == "__module__" and isinstance(n.value, ast.Attribute) \
                        and n.value.attr == "INFRA":
                    out.append((str(p), n.lineno, "INFRA.__module__"))
                elif n.attr in FORBIDDEN_PRIVATE_ATTRS:
                    root_name = n.value
                    while isinstance(root_name, ast.Attribute):
                        root_name = root_name.value
                    if isinstance(root_name, ast.Name) and root_name.id in fae_names:
                        out.append((str(p), n.lineno, f"{ast.unparse(n.value)}.{n.attr}"))
    return sorted(set(out))


class TestSdkBoundary(unittest.TestCase):

    def test_fixture_experiment_has_no_boundary_offenders(self):
        self.assertEqual(boundary_offenders(_EXPERIMENT), [])

    def test_public_sdk_imports_are_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "clean.py").write_text(
                "import fae.experiment\n"
                "from fae.experiment import Gate, config, variants\n"
                "from fae.cell.verify import Ctx, RunnerHelper, Verdict, Verifier, now\n"
                "from fae.cell.substrate import DomainOverlay\n"
                "from fae.cell.infra import base\n"
                "from fae.cell.infra.base import AgentAccess, HookFailure, cksum, host_ports\n"
                "from fae.cell.verify_host import VerifyHostProbe\n"
                "from fae.cell.contrib import elastic_resource\n"
                "def f():\n"
                "    return base.run(['true']), base.ok(['true']), now()\n"
            )
            self.assertEqual(boundary_offenders(d), [])

    def test_detects_forbidden_internals_and_private_symbols(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bad.py").write_text(
                "from fae.cell.cell import Cell\n"
                "from fae.cell import host_sleep\n"
                "import fae.conduct\n"
                "from fae.cell.infra.dind import VerifyDind\n"
                "from fae.cell.infra.kind import KindCluster\n"
                "from fae.cell.infra.secrunner import SecRunner\n"
                "from fae.experiment.variants import _ShimCell\n"
                "from fae.cell import verify\n"
                "from fae.cell.infra import base\n"
                "def f(vcls):\n"
                "    a = verify._now()\n"
                "    b = base._run(['true'])\n"
                "    c = base._ok(['true'])\n"
                "    m = vcls.INFRA.__module__\n"
                "    return a, b, c, m\n"
            )
            got = [(line, what) for _, line, what in boundary_offenders(d)]
            self.assertEqual(got, [
                (1, "from fae.cell.cell"),
                (2, "from fae.cell import host_sleep"),
                (3, "import fae.conduct"),
                (4, "from fae.cell.infra.dind"),
                (5, "from fae.cell.infra.kind"),
                (6, "from fae.cell.infra.secrunner"),
                (7, "from fae.experiment.variants import _ShimCell"),
                (11, "verify._now"),
                (12, "base._run"),
                (13, "base._ok"),
                (14, "INFRA.__module__"),
            ])

    def test_tests_directory_is_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tests").mkdir()
            (Path(d) / "tests" / "test_x.py").write_text("from fae.conduct import Conduct\n")
            self.assertEqual(boundary_offenders(d), [])


if __name__ == "__main__":
    unittest.main()
