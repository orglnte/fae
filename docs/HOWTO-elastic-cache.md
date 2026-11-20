# Building a system-design experiment: elastic cache under load

*Comparing infrastructure approaches (Docker vs Kubernetes) on a stateful web
service judged by a real load test and a deterministic scaling law.*

For a single-container software-design task (CLI program, no sidecars or
clusters), start with [`HOWTO.md`](HOWTO.md). This tutorial shows how to build
a **system-design** experiment where every variant provisions real runtime
infrastructure and every attempt is judged against a live traffic profile.

## What we are building

Agents receive a fixed web service backed by a capped Postgres store and must
author the **elastic cache** slice: mount a Redis cache when sustained load
saturates the store, release it once load drains, and ignore short traffic
blips.

We compare two provisioning surfaces across two access regimes:

1. **Docker + IaC** (`iac_sealed`, `iac_access`): the service drives a
   Docker-in-Docker daemon to spin up and tear down Redis.
2. **Kubernetes + autoscaler** (`k8s_sealed`, `k8s_access`): the agent authors
   Kubernetes manifests and an autoscaler policy applied to a `kind` cluster.

In both families, FAE owns the substrate lifecycle (`[substrate]` in each
variant TOML), the secure runner (`ctx.runner`), auxiliary fixture containers
(`ctx.start_fixture` / `ctx.run_fixture`), and the elastic-resource scaling law
(`fae.cell.contrib.elastic_resource`). The experiment owns only its task, its
thin `DomainOverlay` hooks, its verifier orchestration, and its host probe.

## Contents

1. [Experiment layout](#1-experiment-layout)
2. [The definition (`__init__.py`)](#2-the-definition)
3. [Declaring `[substrate]` in variant TOMLs](#3-declaring-substrate-in-variant-tomls)
4. [Thin domain overlays (`DomainOverlay`)](#4-thin-domain-overlays)
5. [Writing the verifier on `Ctx` and `elastic_resource`](#5-writing-the-verifier)
6. [Verify-host probe and calibration](#6-verify-host-probe-and-calibration)
7. [Host tuning, negative control, and SDK boundary check](#7-host-tuning-negative-control-and-boundary-check)

---

## 1. Experiment layout

```
elastic-cache/
├── cli.py                          # imports ../fae (or $FAE_DIR), sets REPO_ROOT
├── Dockerfile.agent-base           # agent CLIs + common test tools (no variant-specific tools)
└── experiment/
    ├── __init__.py                 # Gate (6 arrangements), CONFIG, verifier_class, verify_host_probe
    ├── agents.toml                 # models compared
    ├── task/
    │   ├── T1.PROMPT.md
    │   └── skeleton/               # fixed web service + README (no variant hints)
    ├── variants/
    │   ├── iac_sealed.toml         # [substrate] kind = "docker", lifecycle = "split", access = false
    │   ├── iac_access.toml         # [substrate] kind = "docker", lifecycle = "split", access = true
    │   ├── k8s_sealed.toml         # [substrate] kind = "k8s",    lifecycle = "split", access = false
    │   ├── k8s_access.toml         # [substrate] kind = "k8s",    lifecycle = "split", access = true
    │   ├── iac/
    │   │   ├── __init__.py         # IacOverlay(DomainOverlay)
    │   │   ├── agent/Dockerfile    # Docker CLI + IaC CLI layer over agent-base
    │   │   ├── verify/Dockerfile   # FROM $BASE + IaC CLI
    │   │   └── seed/               # stub, docs, reference/
    │   └── k8s/
    │       ├── __init__.py         # K8sOverlay(DomainOverlay)
    │       ├── agent/Dockerfile    # kubectl + schema validator layer
    │       ├── verify/Dockerfile   # FROM $BASE + kind + kubectl + helm
    │       └── seed/               # stub, docs, reference/
    ├── verifier/
    │   ├── __init__.py             # ElasticCacheVerifier(Verifier)
    │   ├── probe.py                # StoreProbe(VerifyHostProbe)
    │   └── Dockerfile              # python + docker CLI + k6 + pgbench client
    └── tests/
        └── test_boundary.py        # runs fae's boundary_offenders + cell-file ownership scan
```

## 2. The definition

`experiment/__init__.py` declares the 6-arrangement gate (every permutation of
two spikes `S` and one blip `B`, or two blips and one spike), the host-tuned
load parameters, and the lazy hooks for the verifier and verify-host probe:

```python
from fae.experiment import Gate

NAME = "elastic-cache"

GATE = Gate(
    arrangements=("BBS", "BSB", "SBB", "SSB", "SBS", "BSS"),
    rotate=True,
    feedback_note=(
        "Your build must handle every ordering of short blips (ignore) and "
        "sustained spikes (mount cache, then release when load drains)."
    ),
)

CONFIG = {
    "load": {
        "baseline": 18,
        "top": 360,
        "ramp_s": 20,
        "blip_p99_ms": 50,
        "blip_excess_req": 10,
        "sat_p99_ms": 150,
        "max_mount_s": 30,
    },
    "store": {
        "calibration_min_qps": 40,
    },
}


def verifier_class():
    from .verifier import ElasticCacheVerifier
    return ElasticCacheVerifier


def verify_host_probe():
    from .verifier.probe import StoreProbe
    return StoreProbe
```

## 3. Declaring `[substrate]` in variant TOMLs

Instead of subclassing `Infra` and managing DinD or `kind` containers by hand,
each variant declares a `[substrate]` table. FAE compiles it into a managed
`SubstrateInfra` and hands the verifier a typed `ctx.substrate`
(`DockerContext` or `K8sContext`).

### Docker variant (`variants/iac_access.toml`)

```toml
label = "IaC (access)"
factors = { tech = "iac", access = "access" }

[authoring]
template = ["task/skeleton", "variants/iac/seed/overlay"]
surface = { prefixes = ["provisioning/"], files = ["app/cache_hook.py"] }
tools = "variants/iac/agent"

[authoring.inputs]
"TODO.md" = "task/T1.PROMPT.md"
"docs/api.md" = "variants/iac/seed/api.md"

[verify]
image_dir = "variants/iac/verify"
reference = "variants/iac/seed/reference/overlay"

[verify.run]
command = ["python3", "-m", "app.server"]
serves = 8080

[substrate]
kind = "docker"
lifecycle = "split"          # agent gets its own sandbox; each verify gets a fresh DinD
access = true                # false in iac_sealed.toml (skips agent sandbox completely)
cpus = 1
memory = "1g"
images = ["redis:7-alpine"]
overlay = "variants.iac:IacOverlay"
lock = "iac_access"
lock_slots = 1
```

### Kubernetes variant (`variants/k8s_access.toml`)

```toml
label = "K8s (access)"
factors = { tech = "k8s", access = "access" }

[authoring]
template = ["task/skeleton", "variants/k8s/seed/overlay"]
surface = { prefixes = ["k8s/"], files = ["app/metrics.py"] }
tools = "variants/k8s/agent"

[authoring.inputs]
"TODO.md" = "task/T1.PROMPT.md"
"docs/api.md" = "variants/k8s/seed/api.md"

[verify]
image_dir = "variants/k8s/verify"
reference = "variants/k8s/seed/reference/overlay"

[verify.run]
command = ["python3", "-m", "app.server"]
serves = 8080

[substrate]
kind = "k8s"
lifecycle = "split"          # agent sandbox cluster (when access=true) + fresh verify cluster
access = true                # false in k8s_sealed.toml
cpus = 1
memory = "4g"
cluster_prefix = "ec-k8s-"
images = ["redis:7-alpine"]
overlay = "variants.k8s:K8sOverlay"
lock = "k8s_access"
lock_slots = 1
params = { scaler = "prometheus" }
```

Key guarantees from `lifecycle = "split"`:

1. **Zero bleed between agent and verifier:** an agent with `access = true`
   experiments inside its own cell-lifetime sandbox; every verify arrangement
   gets a fresh verify-scoped DinD (`fae-vdind-<cid>`) or `kind` cluster
   (`ec-k8s-<hash(cid-verify)>`), torn down unconditionally in `finally`.
2. **Remote verify host compatibility:** `lifecycle = "split"` works on remote
   `VERIFY_DOCKER_HOST` machines (whereas `lifecycle = "cell"` is refused at
   preflight when a remote verify host is configured, because a host-local cell
   sidecar is unreachable from a remote daemon).
3. **Docker-only host:** when the host lacks a `kind` binary, `K8sContext`
   automatically runs `kind` and `kubectl` inside the variant's verify image
   via `infra.tool()`.

## 4. Thin domain overlays

A `DomainOverlay` (`fae.cell.substrate.DomainOverlay`) adds only what is
specific to your experiment on top of FAE's substrate. It never imports
`fae.cell.infra.dind`, `fae.cell.infra.kind`, or `fae.cell.infra.secrunner`.

### Docker overlay (`variants/iac/__init__.py`)

```python
from fae.cell.substrate import DomainOverlay


class IacOverlay(DomainOverlay):
    def on_verify_setup(self, substrate, ctx, env):
        # FAE already started the fresh DinD and preloaded redis:7-alpine.
        return {
            "DOCKER_HOST": substrate.docker_host,
            "CACHE_HOST": substrate.host,
        }

    def on_verify_teardown(self, substrate, ctx, env):
        # Wipe inner containers/volumes/networks before FAE removes the DinD.
        substrate.reset()
```

### Kubernetes overlay (`variants/k8s/__init__.py`)

```python
from pathlib import Path
from fae.cell.infra.base import HookFailure
from fae.cell.substrate import DomainOverlay


class K8sOverlay(DomainOverlay):
    def on_verify_setup(self, substrate, ctx, env):
        # FAE already created the cluster, joined ctx.verify_container,
        # capped CPUs/memory, and loaded redis:7-alpine.
        r = substrate.apply(Path(__file__).parent / "manifests" / "operator.yaml")
        if r.returncode != 0:
            raise HookFailure(f"operator install failed: {r.stderr[-300:]}")
        # Rewrite APP_HOST placeholder in the verifier's copy of artifacts (never ctx.artifacts)
        return {
            "KUBECONFIG": str(substrate.kubeconfig),
            "K8S_NETWORK": substrate.network,
            "CACHE_HOST": ctx.verify_container,
        }
```

If an access variant also needs to expose an extra endpoint or read-only config
file to the agent container, return an `AgentAccess(env={...}, mounts=(...))`
from `on_cell_setup(self, substrate, infra)`; FAE merges it with the
substrate's own `AgentAccess`.

## 5. Writing the verifier

Inside the verify container, FAE binds runtime helpers directly onto `ctx`:

- `ctx.variant_def` and `ctx.conf` — the active `Variant` and loaded config.
- `ctx.infra` and `ctx.substrate` — the compiled `SubstrateInfra` and typed
  `DockerContext` or `K8sContext`.
- `ctx.runner` — `RunnerHelper` (`fresh_copy()`, `for_variant(...)`, `stop()`).
- `ctx.start_fixture(...)` and `ctx.run_fixture(...)` — auxiliary containers
  (Postgres store, `pgbench`, `k6`, cache probes) automatically joined to
  `ctx.cell_network` and labeled `fae-cell=<cid>` and `fae-role=fixture` so
  both `verify.main()`'s `finally` block and the zombie reaper clean them up.

```python
from pathlib import Path
from fae.cell.contrib import elastic_resource
from fae.cell.infra.base import HookFailure
from fae.cell.verify import Verdict, Verifier


class ElasticCacheVerifier(Verifier):
    IMAGE_DIR = Path(__file__).parent
    FILES = ("verify.log", "service.deploy.log", "trace.csv", "trace_events.json")
    FEEDBACK_LOGS = ("verify.log", "service.deploy.log")
    RUN_LOG = "service.deploy.log"
    MEASURED_STAGES = ("e2e", "scaling")

    def verify(self, ctx):
        out = Path(ctx.out)
        workdir = ctx.runner.fresh_copy("run")
        scratch = out / "secrun-scratch"
        scratch.mkdir(exist_ok=True)

        # 1. Start the capped Postgres store as a tracked fixture
        store_name = f"fae-store-{ctx.cid}"
        ctx.start_fixture(
            store_name,
            "postgres:16-alpine",
            env={"POSTGRES_PASSWORD": "bench", "POSTGRES_DB": "app"},
            cpus=1.0,
            memory="512m",
        )

        # 2. Bench the store (refund if host is overloaded)
        bench = ctx.run_fixture(
            "postgres:16-alpine",
            argv=("pgbench", "-h", store_name, "-U", "postgres", "-T", "5", "app"),
            env={"PGPASSWORD": "bench"},
        )
        if bench.returncode != 0:
            return Verdict(ok=False, stage="store", why="pgbench failed", charge=False)

        # 3. Provision verify substrate + overlay, then start the judged service
        service_env = {
            "DATABASE_URL": f"postgresql://postgres:bench@{store_name}:5432/app",
            "FAE_WORKDIR": str(workdir),
        }
        try:
            extra_env = ctx.infra.verify_setup(ctx, service_env) or {}
        except HookFailure as exc:
            return Verdict(ok=False, stage="infra", why=str(exc), charge=False)

        service_env.update(extra_env)
        networks = [ctx.cell_network]
        if extra_env.get("K8S_NETWORK"):
            networks.append(extra_env["K8S_NETWORK"])

        runner = ctx.runner.for_variant(
            workdir,
            scratch=scratch,
            env=service_env,
            networks=networks,
        )
        try:
            runner.start()
            # Wait for /health on http://{ctx.runner.name}:8080/health, run E2E,
            # then drive the elastic_resource load shape and judge trace.csv:
            schedule = elastic_resource.load_shape.build(ctx.arrangement, ctx.conf)
            # ... run k6 + sampler -> out/trace.csv, out/trace_events.json ...
            verdict = elastic_resource.law.judge(
                out / "trace.csv",
                out / "trace_events.json",
                schedule,
                ctx.conf,
            )
            return verdict
        finally:
            runner.stop(out / self.RUN_LOG)
            ctx.infra.verify_teardown(ctx, service_env)
            # ctx.stop_fixtures() and ctx.substrate.down() also run in verify.main()'s finally
```

Notice what is **not** in this verifier:

1. No `fae.cell.infra.dind`, `fae.cell.infra.kind`, or `fae.cell.infra.secrunner` imports.
2. No manual `docker rm -f` bookkeeping for Postgres, `pgbench`, DinD, or `kind` — FAE stops fixtures and calls `ctx.substrate.down()` in `finally` and again in `verify.main --teardown` after a crash or timeout.
3. No variant-specific `if/elif` branching for provisioning: the Docker and Kubernetes variants share the exact same verifier loop.

## 6. Verify-host probe and calibration

Timed verdicts depend on the verify host's CPU and I/O speed. Implement
`VerifyHostProbe` (`experiment/verifier/probe.py`) to measure the store floor
on each candidate host:

```python
from fae.cell.verify_host import VerifyHostProbe


class StoreProbe(VerifyHostProbe):
    def measure(self, host, conf, log=print):
        # Run a short pgbench container on `host` via host.docker(...)
        # and return {"store_qps": qps}; raise if below calibration_min_qps.
        ...
```

Before running scored cells on any verify host (local or remote `ssh://`),
calibrate it idle:

```sh
python3 cli.py experiment calibrate --host local --repeats 3
```

FAE records the probe metrics, reference timings, and host CPU baseline in
`fae_verify_timings.toml` (machine-local, gitignored). Any verify on an
uncalibrated host stands the cell down with the exact command above.

## 7. Host tuning, negative control, and boundary check

Before queuing real agents, prove the measurement rig in five steps (see
[`AGENTS.md`](../AGENTS.md) §7):

1. **Preflight:** `python3 cli.py experiment check` prints `READY`.
2. **Smoke one variant:** `python3 cli.py experiment smoke --variants iac_sealed`
   — inspect `trace.csv` so saturation lands inside the spike ramp and every
   blip drains before the reference mounts.
3. **Full gate on every reference:** `python3 cli.py experiment smoke --full-gate`
   — all variants × all 6 arrangements green, zero load errors.
4. **Negative control:** lower the spike ceiling below the store's knee
   (`LOAD_TOP_RPS=60 python3 cli.py experiment smoke --variants iac_sealed`) and
   verify it ends in `FAIL[load-shape]`, refunded (`charge=False`), with
   `ALERT LOAD-SHAPE` in the ledger.
5. **Pin the SDK boundary in your test suite:** import FAE's two AST scans in
   `experiment/tests/` so experiments never drift into engine internals or
   direct cell-file access:

```python
import unittest
from pathlib import Path
from test_cell_files_owner import offenders as cell_file_offenders
from test_sdk_boundary import boundary_offenders

EXP_DIR = Path(__file__).resolve().parents[1]


class TestExperimentBoundaries(unittest.TestCase):
    def test_sdk_boundary(self):
        self.assertEqual(boundary_offenders(EXP_DIR), [])

    def test_cell_file_ownership(self):
        self.assertEqual(
            cell_file_offenders(EXP_DIR, producers=("verifier", "variants")),
            [],
        )
```
