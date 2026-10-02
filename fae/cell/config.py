"""The harness configuration, read once.

`values` is every config key a caller reads: the rig defaults, overlaid with
fae.toml at the root the engine works in (the operator surface; `cli.py experiment
init` writes one with every key and its default) and the process
environment. Most keys carry a built-in default, so a missing config still
reads whole.

`exported` is the subset a CHILD PROCESS inherits — every bring-up script and
tool started from Python is given it, the way a shell that had sourced the
config would hand them down.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:                       # py<3.11
    import tomli as tomllib                        # type: ignore

# Attempts-to-green is the study's dependent variable. A per-cell budget makes
# two cells incomparable, so this is one constant for the whole experiment,
# mirrored in fae/driver/common.py; prepare.py records it into every cell.env.
ATTEMPT_BUDGET = 10

# The engine's own rig defaults; fae.toml [rig] overrides one with an
# UPPERCASE key. The experiment's knobs (a store, a load shape) are its
# CONFIG, read below.
_ENGINE_DEFAULTS = {
    "HB_TICK": "30",
    # An agent container's CPU ceiling: agents run beside the one verify the
    # fleet measures at a time, and an uncapped one (its own tests, a build)
    # takes the cores the measurement runs on. 0: no ceiling.
    "AGENT_CPUS": "1",
    # Optional pinning (docker --cpuset-cpus), off when empty: the measured
    # verify's containers on CPUSET_MEASURED, agents on CPUSET_AGENT, e.g.
    # "0-3" and "4" on a host with cores to spare.
    "CPUSET_MEASURED": "",
    "CPUSET_AGENT": "",
}

# The agent container's name, `<prefix><cell id>`: the engine's own label
# (the sealed run, whatever the experiment), and what the fleet console,
# the supervisor and the reaper recognise an agent by.
AGENT_CONTAINER_PREFIX = "fae-agent-"


def agent_container(cid):
    return f"{AGENT_CONTAINER_PREFIX}{cid}"


# The experiment definition the engine runs: task package, variants, verifier
# surface. Relative to the repo root unless [paths] experiment_dir or the
# environment says otherwise.
DEFAULT_EXPERIMENT_DIR = "experiment"

# The keys a child process (a bring-up script, a docker/kubectl call) reads.
_EXPORT_KEYS = (
    "AGENT_CLI", "EXPERIMENT_DIR", "FP_EXTRA_FILES",
    "REPO_ROOT", "WORKSPACES_DIR", "WORK_SLOTS", "AGENT_IMAGE",
    "AGENT_HOME", "CPUSET_MEASURED",
)

_cache: dict[str, "Config"] = {}
_lock = threading.Lock()


class Config:

    def __init__(self, values, exported):
        self.values = values
        self.exported = exported

    def get(self, key, default=None):
        # The config value, else the environment (runtime knobs the config does
        # not name — LIMIT_RETRY_S, PEAK_RPS, NAMESPACE — reach a reader this
        # way, as they did when the config was a sourced shell script), else
        # the default.
        v = self.values.get(key)
        if v in (None, ""):
            v = os.environ.get(key)
        return default if v in (None, "") else v

    def child_env(self, *extra):
        """The environment a child of this driver should see."""
        env = dict(os.environ)
        env.update(self.exported)
        for d in extra:
            env.update({k: str(v) for k, v in (d or {}).items()})
        return env


TOML = "fae.toml"


def _toml(root):
    """The operator surface. Missing at `root` (a test's scratch tree, say),
    fall back to the engine's own fae.toml, else the built-in defaults —
    every key has a default, so a run without a config still reads whole."""
    for base in (Path(root), Path(__file__).resolve().parents[2]):
        p = base / TOML
        if p.exists():
            return tomllib.loads(p.read_text())
    return {}


# Where an agent's credentials live on this machine when fae.toml names no
# `home` for it: one directory per agent CLI.
AGENT_HOME_DEFAULT = {"claude": ".agent-home/.claude", "agy": ".agent-home/.gemini",
                      "opencode": ".agent-home/.opencode", "testagent": ""}


def render_default_toml(definition, experiment_dir=None):
    """The fae.toml `experiment init` writes: every key the engine reads with its
    default and a comment, the caps for the locks the experiment's variants
    declare, and the machine-local keys the experiment declares (CONFIG),
    each under the section it names. `experiment_dir` is the directory the
    file points the engine at (default: the engine's default)."""
    locks = {}
    for cls in definition.variants.values():
        if cls.LOCK:
            locks.setdefault(cls.LOCK, cls.LOCK_SLOTS)
    declared = {}
    for key, (section, name, default, kind) in definition.config_keys.items():
        declared.setdefault(section, []).append((name, default, key))
    out = [f"# fae.toml — machine-local configuration for this checkout (experiment: {definition.name}).",
           "# Written by `cli.py experiment init` with every key at its default; gitignored: it",
           "# holds your local paths and your agent choice. Never a secret — the agent",
           "# authenticates through its own creds home, never a value here.",
           "",
           "[run]",
           "# The agent a spawn without AGENT= runs (a tag of the experiment's",
           "# agents.toml).",
           'agent = "opus"',
           "# true -> the agent runs with stream-json + --verbose so its turn-by-turn",
           "# tool trace lands in the per-attempt log.",
           "stream_agent = true",
           "",
           "[paths]",
           "# The experiment definition, relative to this root or absolute. Env",
           "# EXPERIMENT_DIR overrides.",
           f'experiment_dir = "{experiment_dir or DEFAULT_EXPERIMENT_DIR}"']
    for name, default, key in declared.get("paths", []):
        out.append(f"# {key}: declared by the experiment; env {key} overrides")
        out.append(f'{name} = "{default}"')
    out += ["", "[slots]",
            "# Concurrency caps enforced by the fd arena: total concurrent cells, and",
            "# one cap per exclusive lock the experiment's variants declare.",
            "work = 8"]
    for lock, n in sorted(locks.items()):
        out.append(f"arm_{lock} = {n}")
    for section, items in declared.items():
        if section == "paths":
            continue                      # rendered inside [paths] above
        out += ["", f"[{section}]"]
        for name, default, key in items:
            out.append(f"# {key}: declared by the experiment")
            out.append(f'{name} = "{default}"')
    out += ["", "# Where each agent's credentials live on this machine (the cell copies",
            "# them into a home of its own). The agents themselves are the",
            "# experiment's agents.toml. Default per CLI: " + ", ".join(
                f"{c} {h}" for c, h in AGENT_HOME_DEFAULT.items() if h) + ".",
            "# [agents.sonnet]",
            '# home = ".agent-home/.claude"']
    return "\n".join(out) + "\n"


def experiment_dir(root, env=None, paths=None):
    """The experiment definition's directory, absolute: EXPERIMENT_DIR in the
    environment, else [paths] experiment_dir, else the default."""
    env = os.environ if env is None else env
    if paths is None:
        paths = _toml(root).get("paths", {})
    rel = env.get("EXPERIMENT_DIR") or paths.get("experiment_dir", DEFAULT_EXPERIMENT_DIR)
    return Path(rel if os.path.isabs(rel) else f"{root}/{rel}")


def agent_for(tag, definition, toml):
    """(cli, model id, effort, credentials home) of the agent `tag`: the
    experiment declares the first three (agents.toml), this machine's
    fae.toml the home (`[agents.<tag>] home`, else the CLI's default). A tag
    the experiment does not declare runs no agent (a stub or reference
    cell): ("", "", None, "")."""
    a = definition.agents.get(tag)
    if not a:
        return "", "", None, ""
    home = ((toml.get("agents") or {}).get(tag) or {}).get("home")
    return a["cli"], a["model"], a.get("effort"), home if home is not None else AGENT_HOME_DEFAULT[a["cli"]]


def _fp_extra_files(root, trees):
    """The source files folded into the anti-gaming fingerprint beside the
    experiment tree: the trees the experiment declares (its SDKs) and the
    Python cell driver (a verify surface — an edit mid-cell would measure some
    arrangements with one verify and the rest with another). FATAL if a tree
    exists but holds no .py, since a shrunk list silently shrinks the guarded
    surface; a tree absent from this machine is skipped."""
    parts = []
    for tree in trees:
        d = Path(tree)
        if not d.is_dir():
            continue
        got = sorted(str(p) for p in d.rglob("*.py"))
        if not got:
            raise RuntimeError(f"FATAL config: {d} exists but has no .py — the "
                               "fingerprint would silently shrink its guarded surface")
        parts += got
    # the engine's own cell package, wherever it is — never under the root:
    # an experiment repo imports the engine and does not contain it
    parts += sorted(str(p) for p in Path(__file__).resolve().parent.rglob("*.py"))
    return " ".join(parts)


# Every env var _build consults; the cache key includes all of them, so a
# changed override (a test's, or a spawn's) never reads a stale config.
_KEY_ENV = (
    "AGENT", "EFFORT", "SMOKE", "SHAPE_GATE", "STREAM_AGENT",
    "WORK_SLOTS", "AGENT_HOME", "AGENT_IMAGE", "RESULTS_DIR",
    "RIG_LOCK_DIR", "VERIFY_LOCK_DIR", "WORKSPACES_DIR", "SMOKE_WORKSPACES_DIR",
    "EXPERIMENT_DIR",
)


def _definition(root, env, toml):
    from . import experiment as _experiment
    return _experiment.for_config(experiment_dir(root, env, toml.get("paths", {})))


def load(root, env=None):
    root = str(Path(root).resolve())
    env = os.environ if env is None else env
    toml = _toml(root)
    definition = _definition(root, env, toml)
    key = root + "\0" + "\0".join(f"{k}={env.get(k, '')}" for k in _KEY_ENV)
    key += "\0" + "\0".join(f"{k}={env.get(k, '')}" for k in sorted(definition.config_keys))
    key += "\0" + "\0".join(f"{k}={v}" for k, v in sorted(env.items()) if k.startswith("ARM_SLOTS_"))
    with _lock:
        if key in _cache:
            return _cache[key]
        cfg = _build(root, env, toml, definition)
        _cache[key] = cfg
        return cfg


def _build(root, env, toml, definition=None):
    definition = definition or _definition(root, env, toml)
    run = toml.get("run", {})
    paths = toml.get("paths", {})
    slots = toml.get("slots", {})
    rig = toml.get("rig", {})

    def path(rel):
        return rel if os.path.isabs(rel) else f"{root}/{rel}"

    agent = env.get("AGENT") or run.get("agent", "opus")
    smoke = env.get("SMOKE", "")
    cli, model_id, agent_effort, agent_home = agent_for(agent, definition, toml)
    effort = env["EFFORT"] if "EFFORT" in env else (agent_effort if agent_effort is not None else "high")

    v = dict(_ENGINE_DEFAULTS)
    v.update({k: str(val) for k, val in rig.items() if k.isupper()})
    v["ATTEMPT_BUDGET"] = str(ATTEMPT_BUDGET)
    # The gate is the experiment's (its GATE); only a smoke cell, a pipeline
    # check that is never scored, may cut it to the seed arrangement.
    v["SHAPE_GATE"] = env.get("SHAPE_GATE", "")
    if v["SHAPE_GATE"] not in ("", "all") and not smoke:
        raise SystemExit("FATAL config: SHAPE_GATE cuts the experiment's gate; "
                         "only a smoke cell (SMOKE=1) may set it")
    v["STREAM_AGENT"] = env["STREAM_AGENT"] if "STREAM_AGENT" in env \
        else ("1" if run.get("stream_agent", True) else "")
    v["AGENT"] = agent
    v["EFFORT"] = effort
    v["AGENT_CLI"] = cli
    v["AGENT_MODEL"] = model_id
    v["AGENT_TEMPERATURE"] = "0"
    v["CELL_PREFIX"] = f"{agent}{'_' + effort if effort else ''}{'_smoke' if smoke else ''}"

    v["WORK_SLOTS"] = str(env.get("WORK_SLOTS") or slots.get("work", 8))
    # ARM_SLOTS_<LOCK>: [slots] arm_<lock> in the config, the environment on
    # top; a lock named by neither caps at the variant's own LOCK_SLOTS.
    for k, val in slots.items():
        if k.startswith("arm_"):
            v[f"ARM_SLOTS_{k[4:].upper()}"] = str(val)
    for k, val in env.items():
        if k.startswith("ARM_SLOTS_") and val:
            v[k] = str(val)

    v["REPO_ROOT"] = root
    v["EXPERIMENT_DIR"] = str(experiment_dir(root, env, paths))
    # The experiment's own machine-local keys: environment, else the toml
    # section it names, else its default (a path default may name the
    # experiment directory).
    for key, (section, name, default, kind) in definition.config_keys.items():
        val = env.get(key) or toml.get(section, {}).get(name)
        if val is None:
            val = str(default).format(experiment=v["EXPERIMENT_DIR"])
        v[key] = path(str(val)) if kind == "path" else str(val)
    v["AGENT_HOME"] = env.get("AGENT_HOME") or (path(agent_home) if agent_home else "")
    # one image forced on every arm (rig tests); empty: each arm runs in its
    # own layer over the base, resolved per cell (fae/cell/image.py)
    v["AGENT_IMAGE"] = env.get("AGENT_IMAGE") or ""
    v["INSTRUMENTS_DIR"] = f"{v['EXPERIMENT_DIR']}/instruments"
    v["TASK_DIR"] = f"{v['EXPERIMENT_DIR']}/task"
    v["RESULTS_DIR"] = env.get("RESULTS_DIR") or f"{root}/results"
    v["RIG_LOCK_DIR"] = env.get("RIG_LOCK_DIR") or f"{root}/workspaces.nosync/.orch/rig-lock"
    v["VERIFY_LOCK_DIR"] = env.get("VERIFY_LOCK_DIR") or f"{root}/workspaces.nosync/.orch/verify-lock"

    if smoke:
        v["WORKSPACES_DIR"] = env.get("SMOKE_WORKSPACES_DIR") or f"{root}/smoke-workspaces.nosync"
    else:
        v["WORKSPACES_DIR"] = env.get("WORKSPACES_DIR") or f"{root}/workspaces.nosync"
    v["FP_EXTRA_FILES"] = _fp_extra_files(root, definition.fingerprint_trees(v))

    exported = {k: v[k] for k in _EXPORT_KEYS if k in v}
    exported.update({k: v[k] for k in definition.config_keys if k in v})
    exported.update({k: val for k, val in v.items() if k.startswith("ARM_SLOTS_")})
    return Config(v, exported)


# --- agent launch + credential staging ----------------------------------------

def opencode_key_file(agent_home):
    """The opencode key path in the agent's credentials home."""
    return Path(agent_home) / "opencode.key"


def agent_cpu_args(conf):
    """The agent container's CPU ceiling and, when pinning is on, its cores."""
    out = []
    cpus = str(conf.get("AGENT_CPUS", _ENGINE_DEFAULTS["AGENT_CPUS"])).strip()
    if cpus and cpus != "0":
        out += [f"--cpus={cpus}"]
    cpuset = str(conf.get("CPUSET_AGENT") or "").strip()
    if cpuset:
        out += [f"--cpuset-cpus={cpuset}"]
    return out


def build_agent_argv(conf, cid, art, home, prompt_file, docker_net="", kube_mount="",
                     feedback="", image=None):
    """The `docker run ...` argv for one agent invocation, built from the
    config — no shell. The per-arm docker flags (network, kubeconfig mount) the
    setup hook emitted are shlex-split in; the prompt is passed as one arg."""
    import shlex
    cli = conf.get("AGENT_CLI", "claude")
    model = conf.get("AGENT_MODEL", "")
    image = conf.get("AGENT_IMAGE") or image or "fae-agent:latest"
    prompt = Path(prompt_file).read_text()
    common = ["docker", "run", "--rm", "--name", agent_container(cid),
              *agent_cpu_args(conf),
              "-v", f"{art}:/workspace", "-w", "/workspace"]
    add_dirs = ["--add-dir", "/workspace"]
    if feedback:
        common += ["-v", f"{feedback}:/feedback:ro"]
        add_dirs += ["--add-dir", "/feedback"]
    net = shlex.split(docker_net) + shlex.split(kube_mount)

    if cli == "agy":
        return (common + ["-v", f"{home}:/home/node/.gemini"] + net
                + [image, "agy", "--sandbox", "--dangerously-skip-permissions",
                   *add_dirs, "--model", model,
                   "--print-timeout", "60m", "--print", prompt])
    if cli == "opencode":
        key = opencode_key_file(conf.get("AGENT_HOME", "")).read_text().strip()
        return (common + ["-v", f"{home}:/home/node/.config/opencode",
                          "-e", f"OPENCODE_API_KEY={key}"] + net
                + [image, "opencode", "run", "--print-logs", "--log-level",
                   "ERROR", "--model", model, prompt])
    if cli == "testagent":
        return (common + ["-v", f"{home}:/home/node/.testagent",
                          "-e", "CELL_ID", "-e", "VARIANT", "-e", "TESTAGENT_PLAN", "-e", "SERVICE_PORT"] + net
                + [image, "python3", "/home/node/.testagent/testagent.py", prompt])
    # claude
    oauth = Path(conf.get("AGENT_HOME", "")) / ".oauth_token"
    tok = []
    if oauth.is_file() and oauth.stat().st_size:
        tok = ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth.read_text().strip()}"]
    effort = conf.get("EFFORT", "")
    stream = conf.get("STREAM_AGENT", "")
    tail = ["claude", "-p", "--dangerously-skip-permissions", "--model", model]
    if feedback:
        tail += ["--add-dir", "/feedback"]
    if effort:
        tail += ["--effort", effort]
    if stream:
        tail += ["--output-format", "stream-json", "--verbose"]
    return (common + ["-v", f"{home}:/home/node/.claude"] + tok + net
            + [image, *tail, prompt])


def _refuse(dest, suffix):
    if not (os.path.isabs(dest) and str(dest).endswith(suffix)):
        raise RuntimeError(f"REFUSING agent-home dest {dest!r} (not an absolute {suffix} path)")


def stage_agent(conf, cli, dest, root):
    """Give the cell its own agent home holding credentials and nothing else —
    a shared home would let each agent read prior agents' memory/transcripts."""
    import shutil
    dest = str(dest)
    home = Path(conf.get("AGENT_HOME", ""))
    if not cli:
        raise RuntimeError(f"no agent {conf.get('AGENT', '')!r} in the experiment's agents.toml")
    if cli == "agy":
        _refuse(dest, "/.agent-gemini")
        src = home
        if not src.is_dir():
            raise RuntimeError(f"no authed {src} — run the one-time container login (README: agy in Docker)")
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        for junk in ("GEMINI.md", "history", "projects.json", "tmp"):
            p = Path(dest) / junk
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        return
    if cli == "opencode":
        _refuse(dest, "/.agent-opencode")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        model = conf.get("AGENT_MODEL", "")
        prov, _, mod = model.partition("/")
        (Path(dest) / "opencode.json").write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            f'  "model": "{model}",\n  "share": "disabled",\n  "autoupdate": false,\n'
            f'  "provider": {{\n    "{prov}": {{ "models": {{ "{mod}": '
            '{ "options": { "temperature": 0 } } } }\n  },\n'
            '  "permission": { "edit": "allow", "bash": "allow", "webfetch": "deny",\n'
            '                  "external_directory": "allow" }\n}\n')
        return
    if cli == "testagent":
        _refuse(dest, "/.agent-testagent")
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest)
        # the scripted agent is the engine's, wherever the cell's root is
        from fae import paths
        shutil.copy(paths.ENGINE / "testagent.py",
                    Path(dest) / "testagent.py")
        from . import experiment as _experiment
        for vid, cls in _experiment.current().variants.items():
            if cls.REFERENCE is not None and Path(cls.REFERENCE).is_dir():
                shutil.copytree(cls.REFERENCE, Path(dest) / "reference" / vid)
        subprocess_chmod(dest)
        return
    # claude
    _refuse(dest, "/.agent-claude")
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    cred = home / ".credentials.json"
    if not cred.is_file():
        raise RuntimeError(
            f"no .credentials.json under {home} — agent not authenticated "
            f"(docker run -it --rm -v {home}:/home/node/.claude "
            f"{conf.get('AGENT_IMAGE') or '<the agent base image>'} claude auth login)")
    d = Path(dest) / ".credentials.json"
    shutil.copy(cred, d)
    os.chmod(d, 0o600)


def subprocess_chmod(dest):
    """The container runs as uid 1000 and writes findings.json into the home."""
    for r, dirs, files in os.walk(dest):
        for n in dirs + files:
            try:
                os.chmod(os.path.join(r, n), 0o777)
            except OSError:
                pass
    os.chmod(dest, 0o777)


def client_reported_seconds(cli, log):
    """The agent client's own account of how long its run took, for clients
    whose transcript carries one (claude's closing result event); None for
    the others and for a transcript without it."""
    if cli != "claude":
        return None
    try:
        lines = Path(log).read_text(errors="replace").splitlines()[-50:]
    except OSError:
        return None
    for line in reversed(lines):
        if '"result"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            ms = event.get("duration_ms")
            return ms / 1000 if isinstance(ms, (int, float)) else None
    return None
