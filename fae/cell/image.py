"""Images the engine builds and runs.

A Verifier or a Variant declares a DIRECTORY holding a Dockerfile, never a
tag: the tag is `fae-<experiment>-<leaf>:<sha12>` over the Dockerfile,
every file beside it and every staged context entry, so a changed pin or a
changed copied source is a different image, built when missing and never
rebuilt otherwise. A variant's image builds `FROM $BASE`, the verifier's.

The verify container (`fae-verify-<cid>`) and the cell network
(`fae-net-<cid>`) are the engine's own substrate, named here.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TAG_PREFIX = "fae-"
VERIFY_PREFIX = "fae-verify-"
TOOL_PREFIX = "fae-tool-"
RUN_PREFIX = "fae-secrun-"     # the judged program's own container (substrate/secrunner.py)
NET_PREFIX = "fae-net-"
_SKIP = {".git", "__pycache__", ".pytest_cache", ".venv", ".mypy_cache"}
# the host, by the name Docker Desktop gives it, on Linux too: what a
# port published on the host's loopback answers at from a container
HOST_ALIAS = ("--add-host", "host.docker.internal:host-gateway")
# The container runs as the operator's uid, which has no passwd entry in
# the image; a tool that looks its user up (pulumi's Go runtime) needs the
# name from the environment.
CONTAINER_USER = "fae"


def verify_container(cid):
    return f"{VERIFY_PREFIX}{cid}"


def cell_network(cid):
    return f"{NET_PREFIX}{cid}"


def _files(path):
    """(relative name, absolute path) of every file under `path`, sorted; a
    file is itself."""
    path = Path(path)
    if path.is_file():
        return [(path.name, path)]
    out = []
    for p in sorted(path.rglob("*")):
        if p.is_file() and not (set(p.relative_to(path).parts) & _SKIP) \
                and not p.name.endswith((".pyc", ".pyo")):
            out.append((str(p.relative_to(path)), p))
    return out


def content_hash(dockerfile_dir, context=(), base=None):
    """sha256 over the Dockerfile directory, the staged context and the base
    tag: what `docker build` will see, and nothing else."""
    h = hashlib.sha256()
    h.update(f"base={base or ''}\n".encode())
    for rel, p in _files(dockerfile_dir):
        h.update(f"dir/{rel}\n".encode())
        h.update(p.read_bytes())
    for src, dest in context:
        for rel, p in _files(src):
            h.update(f"ctx/{dest}/{rel}\n".encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def tag(name, dockerfile_dir, context=(), base=None):
    if not (Path(dockerfile_dir) / "Dockerfile").is_file():
        raise RuntimeError(f"no Dockerfile in {dockerfile_dir}")
    return f"{TAG_PREFIX}{name}:{content_hash(dockerfile_dir, context, base)[:12]}"


def present(image):
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


def build(image, dockerfile_dir, context=(), base=None, log=print, labels=()):
    """`docker build` the staged tree: the Dockerfile directory's files plus
    each context entry copied under its `dest` name. Raises on failure."""
    with tempfile.TemporaryDirectory(prefix="fae-build-") as tmp:
        stage = Path(tmp)
        for rel, p in _files(dockerfile_dir):
            (stage / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, stage / rel)
        for src, dest in context:
            src = Path(src)
            if src.is_file():
                (stage / dest).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, stage / dest)
            else:
                shutil.copytree(src, stage / dest, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(*_SKIP, "*.pyc"))
        argv = ["docker", "build", "-t", image]
        if base:
            argv += ["--build-arg", f"BASE={base}"]
        for k, v in labels:
            argv += ["--label", f"{k}={v}"]
        log(f"image: building {image} from {dockerfile_dir}")
        r = subprocess.run(argv + [str(stage)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"docker build failed for {image}:\n{(r.stderr or r.stdout)[-2000:]}")
    log(f"image: built {image}")


def ensure(name, dockerfile_dir, context=(), base=None, log=print):
    """The image's tag, built first when the daemon does not have it."""
    image = tag(name, dockerfile_dir, context, base)
    if not present(image):
        build(image, dockerfile_dir, context, base, log)
    return image


# --- what the definition declares -------------------------------------------

def for_verifier(definition, conf, log=print):
    """The experiment verifier's image: its tools and the engine's runtime."""
    cls = definition.verifier_class()
    if not cls.IMAGE_DIR:
        raise RuntimeError(f"{cls.__name__} declares no IMAGE_DIR: the verifier "
                           f"runs only in a container")
    return ensure(f"{definition.name}-verifier", cls.IMAGE_DIR,
                  cls.image_context(conf), log=log)


def for_variant(variant_cls, definition, conf, log=print):
    """The image a cell of this variant is verified in: the variant's own,
    layered on the verifier's, or the verifier's when it declares none."""
    base = for_verifier(definition, conf, log)
    if not variant_cls.IMAGE_DIR:
        return base
    return ensure(f"{definition.name}-{variant_cls.TECH}", variant_cls.IMAGE_DIR,
                  variant_cls.image_context(conf), base=base, log=log)


# --- the agent image -------------------------------------------------------------

BASE_AGENT = "fae-agent:latest"     # the engine's default base: fae/agent-container/Dockerfile
BASE_DOCKERFILE = "Dockerfile.agent-base"   # an experiment's own base, at its root
CONTENT_LABEL = "fae-content"       # what a moving tag was built from


def image_id(image):
    r = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def client_versions(image, cache_file):
    """{cli: version} the agent image carries. Keyed by image id, so a rebuilt
    tag is probed afresh: one container run per new id, a cache read after."""
    iid = image_id(image)
    if not iid:
        return {}
    cache_file = Path(cache_file)
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}
    if iid in cache:
        return cache[iid]
    from fae.driver import image as _clients
    found = _clients.installed(iid)
    if found:
        cache[iid] = found
        tmp = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(cache, sort_keys=True))
            os.replace(tmp, cache_file)
        except OSError:
            pass
    return found


def label(image, key):
    r = subprocess.run(["docker", "image", "inspect", "-f", '{{index .Config.Labels "%s"}}' % key,
                        image], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def base_dockerfile(root):
    """The Dockerfile of the agents' base image (every model's client, git,
    python3): the experiment's own at its root when it has one, else the
    engine's."""
    from fae import paths
    own = Path(root) / BASE_DOCKERFILE
    return own if own.is_file() else paths.ENGINE / "agent-container" / "Dockerfile"


def base_tag(definition, root):
    """The base image's tag: the experiment's when it has its own base."""
    own = Path(root) / BASE_DOCKERFILE
    return f"{TAG_PREFIX}{definition.name}-agent-base:latest" if own.is_file() else BASE_AGENT


def agent_tag(definition, root, variant_cls=None):
    """The image an arm's agents run in: its variant's layer over the base,
    one per arm family (TECH), so an agent sees its own arm's tools and no
    other's; the base when the variant declares no layer. A moving tag: the
    clients inside the base are kept at upstream latest."""
    if variant_cls is not None and variant_cls.AGENT_IMAGE_DIR:
        return f"{TAG_PREFIX}{definition.name}-agent-{variant_cls.TECH}:latest"
    return base_tag(definition, root)


def for_agent(definition, conf, variant_cls, root, log=print):
    """The arm's agent image, its layer built when the tag does not carry
    what it would be built from now: the variant's Dockerfile directory, its
    staged context, the base image's id (a rebuilt base rebuilds the layer)."""
    base_ = base_tag(definition, root)
    tag_ = agent_tag(definition, root, variant_cls)
    if tag_ == base_:
        return base_
    base = image_id(base_)
    if not base:
        raise RuntimeError(f"{base_} is not present: build the base first "
                           f"(cli.py rig agent-image)")
    context = variant_cls.agent_image_context(conf)
    want = content_hash(variant_cls.AGENT_IMAGE_DIR, context, base=base)
    if label(tag_, CONTENT_LABEL) != want:
        build(tag_, variant_cls.AGENT_IMAGE_DIR, context, base=base_, log=log,
              labels=((CONTENT_LABEL, want),))
    return tag_


# --- the cell network ----------------------------------------------------------

def network_up(cid):
    net = cell_network(cid)
    if subprocess.run(["docker", "network", "inspect", net], capture_output=True).returncode:
        r = subprocess.run(["docker", "network", "create", "--label", f"fae-cell={cid}", net],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"could not create {net}: {r.stderr.strip()}")
    return net


def network_down(cid):
    subprocess.run(["docker", "network", "rm", cell_network(cid)], capture_output=True)


def remove_container(name):
    subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)


# --- running a tool of an image ----------------------------------------------

_ENV_DENY = ("PATH", "HOME", "PYTHONPATH", "TMPDIR", "SHELL", "PWD", "OLDPWD", "_",
             "SHLVL", "TERM", "LANG", "LC_ALL", "SSH_AUTH_SOCK", "DOCKER_HOST",
             "VIRTUAL_ENV", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE")
# a host path a library would open at startup (the host's CA bundle): the
# image has its own
_ENV_DENY_INFIX = ("_CERT_FILE", "_CERT_DIR", "CA_BUNDLE")
_SECRET = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")


def child_env(base_env, **extra):
    """The environment a container of the engine's gets: the driver's, minus
    the host's own process facts (its paths, its interpreter, its CA
    bundle) and anything that names a secret, plus `extra`. HOME and PATH
    are the image's."""
    env = {}
    for k, v in base_env.items():
        if k in _ENV_DENY or any(s in k.upper() for s in _ENV_DENY_INFIX + _SECRET):
            continue
        env[k] = v
    env.update({k: str(v) for k, v in extra.items() if v is not None})
    return env


def socket_group():
    """The group that may use the daemon's socket: the host socket's own
    where the host exposes one; root's where the socket is the VM's (Docker
    Desktop mounts it root:root, group-writable)."""
    try:
        return os.stat("/var/run/docker.sock").st_gid
    except OSError:
        return 0


def run_argv(image, name, argv, mounts=(), env=None, workdir=None, network=None,
             user=True, socket=True, labels=(), extra=(), cpus=None, cpuset=None):
    """`docker run --rm` of `argv` in `image`: every mount at its own host
    path, so paths in the ctx mean the same inside; the daemon's socket for
    what the tool provisions; the caller's uid so files are the operator's,
    with the socket's group so the daemon still answers; `cpus` caps the
    container's CPU time (docker --cpus)."""
    out = ["docker", "run", "--rm", "--name", name]
    if cpus is not None:
        out += [f"--cpus={cpus}"]
    if cpuset:
        out += [f"--cpuset-cpus={cpuset}"]
    if user:
        out += ["--user", f"{os.getuid()}:{os.getgid()}"]
        if socket:
            out += ["--group-add", str(socket_group())]
    for k, v in labels:
        out += ["--label", f"{k}={v}"]
    if network:
        out += ["--network", network]
    seen = set()
    for m in mounts:
        m = str(m)
        if m and m not in seen:
            seen.add(m)
            out += ["-v", f"{m}:{m}"]
    if socket:
        out += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    for k, v in sorted((env or {}).items()):
        out += ["-e", f"{k}={v}"]
    if workdir:
        out += ["-w", str(workdir)]
    out += list(extra)
    return out + [image] + list(argv)


def mounts_for(*paths):
    """The minimal set of host paths that covers `paths`: a path under
    another is not mounted twice. Paths are mounted AS WRITTEN (absolute,
    symlinks kept): the ctx names them, and inside they must read the same."""
    ps = sorted({os.path.abspath(p) for p in paths if p})
    out = []
    for p in ps:
        if not any(p == q or p.startswith(q.rstrip("/") + "/") for q in out):
            out.append(p)
    return out
