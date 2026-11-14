"""`fae_verify_timings.toml`: what each verify host was calibrated to, one
section per host, in the experiment root beside `fae.toml`.

A section records, for one host (its `VERIFY_DOCKER_HOST` entry, `local` for
the daemon the authoring runs on): when and from which commits it was
calibrated, the load values its verifies run with, the probe's numbers
(`verify_host_probe()`), and for each (variant, arrangement) the reference
build's named values with their spread. The spread is `estimated` from one
run, or `measured` from `n` >= 2 runs. The engine keeps the names opaque;
the experiment's verifier and its law give them meaning.

    [hosts."ssh://operator@verify1"]
    date = "2026-11-14T10:00:00Z"
    fae_commit = "9d29b87"
    experiment_commit = "8c9598d"
    load = { LOAD_TOP_RPS = "266" }
    probe = { store_qps = 96.0 }

    [[hosts."ssh://operator@verify1".references]]
    variant = "pulumi_sealed_apidocs"
    arrangement = "BSB"
    spread = "estimated"
    n = 1
    values = { sat_s = 6.6, mount_s = 9.0 }
    spreads = { sat_s = 1.0, mount_s = 2.0 }
"""
from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

FILE = "fae_verify_timings.toml"
LOCAL = "local"
SPREADS = ("estimated", "measured")

_HOST_KEYS = {"date", "fae_commit", "experiment_commit", "load", "probe", "references"}
_REF_KEYS = {"variant", "arrangement", "spread", "n", "values", "spreads"}
_BARE = re.compile(r"^[A-Za-z0-9_-]+$")


class TimingsError(ValueError):
    pass


def host_key(host):
    """The section name of a VerifyHost: its daemon, `local` for the local one."""
    return host.docker_host or LOCAL


@dataclass(frozen=True)
class Reference:
    variant: str
    arrangement: str
    values: dict
    spreads: dict
    spread: str = "estimated"
    n: int = 1


@dataclass(frozen=True)
class HostTimings:
    host: str
    date: str
    fae_commit: str
    experiment_commit: str
    load: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)
    references: tuple = ()

    def reference(self, variant, arrangement):
        for r in self.references:
            if (r.variant, r.arrangement) == (variant, arrangement):
                return r
        return None


class VerifyTimings:
    def __init__(self, hosts=None):
        self.hosts = dict(hosts or {})

    @classmethod
    def read(cls, root):
        """The file at `root`; no file is no calibrated host."""
        path = Path(root) / FILE
        if not path.exists():
            return cls()
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise TimingsError(f"{path}: {e}") from e
        unknown = set(data) - {"hosts"}
        if unknown:
            raise TimingsError(f"{path}: unknown key(s) {sorted(unknown)}")
        return cls({name: _section(path, name, sec) for name, sec in (data.get("hosts") or {}).items()})

    def host(self, name):
        return self.hosts.get(name)

    def with_host(self, timings):
        """A copy with `timings` as its host's section, every other kept."""
        return VerifyTimings({**self.hosts, timings.host: timings})

    def write(self, root):
        path = Path(root) / FILE
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(self.render())
        os.replace(tmp, path)

    def render(self):
        out = ["# Written by `cli.py experiment calibrate`; one section per verify host.\n"]
        for name in sorted(self.hosts):
            h = self.hosts[name]
            sec = f"hosts.{_key(name)}"
            out.append(f"\n[{sec}]\n")
            for k in ("date", "fae_commit", "experiment_commit"):
                out.append(f"{k} = {_str(getattr(h, k))}\n")
            out.append(f"load = {_inline(h.load, str)}\n")
            out.append(f"probe = {_inline(h.probe, float)}\n")
            for r in h.references:
                out.append(f"\n[[{sec}.references]]\n")
                out.append(f"variant = {_str(r.variant)}\narrangement = {_str(r.arrangement)}\n")
                out.append(f"spread = {_str(r.spread)}\nn = {int(r.n)}\n")
                out.append(f"values = {_inline(r.values, float)}\n")
                out.append(f"spreads = {_inline(r.spreads, float)}\n")
        return "".join(out)


def _section(path, name, sec):
    where = f"{path} [hosts.{_key(name)}]"
    unknown = set(sec) - _HOST_KEYS
    if unknown:
        raise TimingsError(f"{where}: unknown key(s) {sorted(unknown)}")
    missing = [k for k in ("date", "fae_commit", "experiment_commit") if not sec.get(k)]
    if missing:
        raise TimingsError(f"{where}: missing {', '.join(missing)}")
    refs = tuple(_reference(where, i, r) for i, r in enumerate(sec.get("references") or []))
    seen = [(r.variant, r.arrangement) for r in refs]
    if len(seen) != len(set(seen)):
        raise TimingsError(f"{where}: a (variant, arrangement) appears twice")
    return HostTimings(host=name, date=str(sec["date"]), fae_commit=str(sec["fae_commit"]),
                       experiment_commit=str(sec["experiment_commit"]),
                       load={k: str(v) for k, v in (sec.get("load") or {}).items()},
                       probe={k: float(v) for k, v in (sec.get("probe") or {}).items()},
                       references=refs)


def _reference(where, i, r):
    where = f"{where} references[{i}]"
    unknown = set(r) - _REF_KEYS
    if unknown:
        raise TimingsError(f"{where}: unknown key(s) {sorted(unknown)}")
    if not r.get("variant") or not r.get("arrangement"):
        raise TimingsError(f"{where}: variant and arrangement are required")
    spread, n = r.get("spread", "estimated"), int(r.get("n", 1))
    if spread not in SPREADS:
        raise TimingsError(f"{where}: spread is one of {SPREADS}, got {spread!r}")
    if spread == "measured" and n < 2:
        raise TimingsError(f"{where}: a measured spread needs n >= 2 runs, got {n}")
    values = {k: float(v) for k, v in (r.get("values") or {}).items()}
    spreads = {k: float(v) for k, v in (r.get("spreads") or {}).items()}
    extra = set(spreads) - set(values)
    if extra:
        raise TimingsError(f"{where}: spreads for no value: {sorted(extra)}")
    return Reference(variant=r["variant"], arrangement=r["arrangement"], values=values,
                     spreads=spreads, spread=spread, n=n)


def _key(name):
    return name if _BARE.match(name) else _str(name)


def _str(s):
    return json.dumps(str(s))


def _inline(d, kind):
    if kind is float and not all(math.isfinite(float(v)) for v in d.values()):
        raise TimingsError(f"not a finite number in {d}")
    body = ", ".join(f"{_key(k)} = {_str(v) if kind is str else repr(float(v))}"
                     for k, v in sorted(d.items()))
    return "{ " + body + " }" if body else "{}"
