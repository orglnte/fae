"""The agents' images and the client CLIs they carry.

Three clients ride in the base image: claude (npm), opencode (npm) and agy
(Google's installer, a per-platform manifest). The base is built from the
experiment root's Dockerfile.agent-base, else the engine's
fae/agent-container/Dockerfile. Each provider gates new models on a minimum
client version, so a stale image fails every cell of a lane with a 400 that
looks like a wall. `ensure_current` compares the installed versions with
upstream (cached for CHECK_TTL_S) and rebuilds the base when any is behind;
`ensure_agent` does that and then builds each arm's layer over it
(fae/cell/image.py: for_agent) — every arm's agents run in their own.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import urllib.request
import ujson as json

from fae.driver import common

CHECK_TTL_S = 3600
AGY_MANIFEST = ("https://antigravity-cli-auto-updater-974169037036"
                ".us-central1.run.app/manifests/linux_{arch}.json")
NPM = {"claude": "@anthropic-ai/claude-code", "opencode": "opencode-ai"}
TOOLS = ("claude", "opencode", "agy")
BUILD_ARG = {"claude": "CLAUDE_CODE_VERSION", "opencode": "OPENCODE_VERSION",
             "agy": "AGY_VERSION"}
_VER = re.compile(r"\d+(?:\.\d+)+")


def image_name():
    """The base image: the clients live there, whatever layer a cell runs in."""
    from fae.cell import image as _image
    return _image.base_tag(common.definition(), common.ROOT)


def cache_path():
    return common.ORCH / "agent_image.json"


def vtuple(v):
    return tuple(int(x) for x in v.split("."))


def parse_versions(text):
    """`tool=<line>` lines from the in-image probe -> {tool: version}."""
    out = {}
    for line in text.splitlines():
        tool, _, rest = line.partition("=")
        m = _VER.search(rest)
        if tool in TOOLS and m:
            out[tool] = m.group(0)
    return out


def installed(img=None):
    img = img or image_name()
    probe = "; ".join(f"echo {t}=$({t} --version 2>&1 | tail -n1)" for t in TOOLS)
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", img, "-c", probe],
                       capture_output=True, text=True)
    return parse_versions(r.stdout) if r.returncode == 0 else {}


def image_arch(img=None):
    r = subprocess.run(["docker", "image", "inspect", img or image_name(),
                        "--format", "{{.Architecture}}"], capture_output=True, text=True)
    return r.stdout.strip() or "arm64"


def upstream(arch="arm64"):
    """Latest version per tool; a tool whose lookup fails is absent."""
    out = {}
    for tool, pkg in NPM.items():
        r = subprocess.run(["npm", "view", pkg, "version"], capture_output=True, text=True)
        m = _VER.search(r.stdout) if r.returncode == 0 else None
        if m:
            out[tool] = m.group(0)
    try:
        with urllib.request.urlopen(AGY_MANIFEST.format(arch=arch), timeout=15) as f:
            v = json.loads(f.read()).get("version", "")
        if _VER.fullmatch(v):
            out["agy"] = v
    except Exception:
        pass
    return out


def upstream_cached(arch="arm64", now=None, ttl=CHECK_TTL_S):
    now = now if now is not None else time.time()
    p = cache_path()
    try:
        c = json.loads(p.read_text())
        if now - c["checked_at"] < ttl and c.get("arch") == arch:
            return c["upstream"]
    except Exception:
        pass
    up = upstream(arch)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"checked_at": now, "arch": arch, "upstream": up}))
    return up


def stale(have, want):
    """[(tool, installed, latest)] for every tool behind upstream."""
    out = []
    for tool in TOOLS:
        h, w = have.get(tool), want.get(tool)
        if h and w and vtuple(h) < vtuple(w):
            out.append((tool, h, w))
    return out


def rebuild(want, img=None):
    """Build the base image with the wanted versions as build args. Returns rc."""
    from fae.cell import image as _image
    dockerfile = _image.base_dockerfile(common.ROOT)
    args = []
    for tool, v in want.items():
        if tool in BUILD_ARG:
            args += ["--build-arg", f"{BUILD_ARG[tool]}={v}"]
    return subprocess.run(["docker", "build", *args, "-f", str(dockerfile),
                           "-t", img or image_name(), str(dockerfile.parent)],
                          cwd=common.ROOT).returncode


def ensure_current(img=None, log=print):
    """Rebuild the image when any client is behind upstream.

    True when the image is current afterwards (or upstream is unreachable —
    an unknown never blocks a spawn); False when a needed rebuild failed."""
    img = img or image_name()
    have = installed(img)
    want = upstream_cached(image_arch(img))
    behind = stale(have, want)
    if not behind:
        return True
    log("agent image behind upstream: " + ", ".join(f"{t} {h} < {w}" for t, h, w in behind)
        + " — rebuilding")
    rc = rebuild(want, img)
    if rc != 0:
        log(f"agent image rebuild FAILED (rc={rc}); cells keep the old image")
        return False
    still = stale(installed(img), want)
    if still:
        log("agent image still behind after rebuild: "
            + ", ".join(f"{t} {h} < {w}" for t, h, w in still))
        return False
    log("agent image rebuilt: " + ", ".join(f"{t} {v}" for t, v in sorted(installed(img).items())))
    return True


def arm_layers():
    """{tag: variant class}: one agent layer per arm family in the matrix."""
    from fae.cell import image as _image
    d = common.definition()
    out = {}
    for arm in d.matrix:
        cls = d.variant(arm)
        if cls is not None and cls.AGENT_IMAGE_DIR:
            out.setdefault(_image.agent_tag(d, common.ROOT, cls), cls)
    return out


def ensure_agent(log=print):
    """Every image a spawn may use, ready: the base present (built when
    missing) with its clients current, then each arm's layer over it,
    rebuilt when its content or the base moved. False when a build failed —
    every spawn would die at preflight."""
    from fae.cell import config as _config
    from fae.cell import image as _image
    base = image_name()
    if not _image.present(base):
        log(f"agent image {base} is missing — building it")
        rc = rebuild({}, base)
        if rc != 0:
            log(f"agent image build FAILED (rc={rc})")
            return False
        log(f"built {base}")
    if not ensure_current(base, log):
        return False
    conf = _config.load(common.ROOT)
    for tag, cls in arm_layers().items():
        try:
            _image.for_agent(common.definition(), conf, cls, common.ROOT, log=log)
        except RuntimeError as e:
            log(f"agent layer {tag}: {e}")   # a build failure carries the build's tail
            return False
        log(f"agent image: {tag}")
    return True


def ensure_agent_for(arm, log=print):
    """The image a cell of `arm` runs its agent in, ready: the base (built
    when missing), then the arm's layer over it. False when a build failed."""
    from fae.cell import config as _config
    from fae.cell import image as _image
    base = image_name()
    if not _image.present(base):
        log(f"agent image {base} is missing — building it")
        if rebuild({}, base) != 0:
            log(f"agent image build FAILED: {base}")
            return False
    cls = common.definition().variant(arm)
    if cls is None or not cls.AGENT_IMAGE_DIR:
        return True
    try:
        _image.for_agent(common.definition(), _config.load(common.ROOT), cls,
                         common.ROOT, log=log)
    except RuntimeError as e:
        log(f"agent layer for {arm}: {e}")
        return False
    return True


def report(img=None):
    """Print installed vs latest per tool, and the layer's tag. Returns the
    stale list."""
    img = img or image_name()
    have, want = installed(img), upstream(image_arch(img))
    behind = {t for t, _, _ in stale(have, want)}
    for t in TOOLS:
        mark = "BEHIND" if t in behind else ("ok" if have.get(t) else "?")
        print(f"  {t:9} installed {have.get(t, '-'):10} latest {want.get(t, '?'):10} {mark}")
    from fae.cell import image as _image
    for layer in arm_layers():
        print(f"  layer     {layer:32} {'present' if _image.present(layer) else 'MISSING'}")
    return stale(have, want)
