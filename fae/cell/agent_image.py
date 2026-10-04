"""The images the agents run in: the base, which carries every client CLI,
and each variant's tools layer over it.

Three clients ride in the base: claude (npm), opencode (npm) and agy
(Google's installer, a per-platform manifest). Each provider gates new models
on a minimum client version, so a stale base fails every cell of a lane with a
400 that looks like a wall. The base is built from the experiment root's
Dockerfile.agent-base, else the engine's fae/agent-container/Dockerfile; a
variant's layer ([authoring] tools) is built over it, so an agent sees its own
variant's tools and no other's.

`AgentImage.ready` is the one readiness policy — the base present, its
clients current, the variant's layer built — and runs under .locks/image-lock,
so concurrent callers build once. The books it keeps live in .images/.
"""
from __future__ import annotations

import re
import subprocess
import time
import urllib.request
from pathlib import Path

import json

from fae import mutex, plane
from fae.queues import Book
from . import image as _image

CHECK_TTL_S = 3600
AGY_MANIFEST = ("https://antigravity-cli-auto-updater-974169037036"
                ".us-central1.run.app/manifests/linux_{arch}.json")
NPM = {"claude": "@anthropic-ai/claude-code", "opencode": "opencode-ai"}
TOOLS = ("claude", "opencode", "agy")
BUILD_ARG = {"claude": "CLAUDE_CODE_VERSION", "opencode": "OPENCODE_VERSION",
             "agy": "AGY_VERSION"}
_VER = re.compile(r"\d+(?:\.\d+)+")

BASE_AGENT = "fae-agent:latest"     # the engine's default base: fae/agent-container/Dockerfile
BASE_DOCKERFILE = "Dockerfile.agent-base"   # an experiment's own base, at its root
CONTENT_LABEL = "fae-content"       # what a moving tag was built from


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


def stale(have, want):
    """[(tool, installed, latest)] for every tool behind upstream."""
    out = []
    for tool in TOOLS:
        h, w = have.get(tool), want.get(tool)
        if h and w and vtuple(h) < vtuple(w):
            out.append((tool, h, w))
    return out


def image_id(image):
    r = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", image],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


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
    return f"{_image.TAG_PREFIX}{definition.name}-agent-base:latest" if own.is_file() else BASE_AGENT


def agent_tag(definition, root, variant_cls=None):
    """The image a variant's agents run in: its tools layer over the base, one
    per layer directory; the base when the variant names none. A moving tag:
    the clients inside the base are kept at upstream latest."""
    if variant_cls is not None and variant_cls.AGENT_IMAGE_DIR:
        name = _image.dir_tag_name(definition, variant_cls.AGENT_IMAGE_DIR)
        return f"{_image.TAG_PREFIX}{name}:latest"
    return base_tag(definition, root)


def for_agent(definition, conf, variant_cls, root, log=print):
    """The variant's agent image, its layer built when the tag does not carry
    what it would be built from now: the variant's Dockerfile directory, its
    staged context, the base image's id (a rebuilt base rebuilds the layer)."""
    base_ = base_tag(definition, root)
    tag_ = agent_tag(definition, root, variant_cls)
    if tag_ == base_:
        return base_
    base = image_id(base_)
    if not base:
        raise RuntimeError(f"{base_} is not present: build the base first")
    context = variant_cls.INFRA.agent_image_context(conf)
    want = _image.content_hash(variant_cls.AGENT_IMAGE_DIR, context, base=base)
    if label(tag_, CONTENT_LABEL) != want:
        _image.build(tag_, variant_cls.AGENT_IMAGE_DIR, context, base=base_, log=log,
                     labels=((CONTENT_LABEL, want),))
    return tag_


class AgentImage:
    """The agents' images of one experiment root."""

    def __init__(self, root, definition, conf=None):
        self.root = Path(root)
        self.definition = definition
        self._conf = conf
        self.lock = plane.locks(self.root) / "image-lock"
        books = plane.images(self.root)
        self.upstream_book = Book(books / "agent_image.json")
        self.clients_book = Book(books / "agent_clients.json")

    @property
    def conf(self):
        if self._conf is None:
            from fae.experiment import config as _config
            self._conf = _config.load(self.root)
        return self._conf

    def base(self):
        return base_tag(self.definition, self.root)

    def tag(self, variant_cls=None):
        return agent_tag(self.definition, self.root, variant_cls)

    def installed(self, img=None):
        img = img or self.base()
        probe = "; ".join(f"echo {t}=$({t} --version 2>&1 | tail -n1)" for t in TOOLS)
        r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", img, "-c", probe],
                           capture_output=True, text=True)
        return parse_versions(r.stdout) if r.returncode == 0 else {}

    def arch(self, img=None):
        r = subprocess.run(["docker", "image", "inspect", img or self.base(),
                            "--format", "{{.Architecture}}"], capture_output=True, text=True)
        return r.stdout.strip() or "arm64"

    @staticmethod
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

    def upstream_cached(self, arch="arm64", now=None, ttl=CHECK_TTL_S):
        now = now if now is not None else time.time()
        try:
            c = self.upstream_book.load()
            if now - c["checked_at"] < ttl and c.get("arch") == arch:
                return c["upstream"]
        except Exception:
            pass
        up = self.upstream(arch)
        self.upstream_book.save({"checked_at": now, "arch": arch, "upstream": up})
        return up

    def rebuild(self, want, img=None):
        """Build the base image with the wanted versions as build args. Returns rc."""
        dockerfile = base_dockerfile(self.root)
        args = []
        for tool, v in want.items():
            if tool in BUILD_ARG:
                args += ["--build-arg", f"{BUILD_ARG[tool]}={v}"]
        return subprocess.run(["docker", "build", *args, "-f", str(dockerfile),
                               "-t", img or self.base(), str(dockerfile.parent)],
                              cwd=self.root).returncode

    def ensure_current(self, img=None, log=print):
        """Rebuild the base when any client is behind upstream.

        True when the image is current afterwards (or upstream is unreachable —
        an unknown never blocks a start); False when a needed rebuild failed."""
        img = img or self.base()
        have = self.installed(img)
        want = self.upstream_cached(self.arch(img))
        behind = stale(have, want)
        if not behind:
            return True
        log("agent image behind upstream: " + ", ".join(f"{t} {h} < {w}" for t, h, w in behind)
            + " — rebuilding")
        rc = self.rebuild(want, img)
        if rc != 0:
            log(f"agent image rebuild FAILED (rc={rc}); cells keep the old image")
            return False
        still = stale(self.installed(img), want)
        if still:
            log("agent image still behind after rebuild: "
                + ", ".join(f"{t} {h} < {w}" for t, h, w in still))
            return False
        log("agent image rebuilt: " + ", ".join(f"{t} {v}" for t, v in sorted(self.installed(img).items())))
        return True

    def layers(self):
        """{tag: variant class}: one agent layer per tools directory the active
        variants name."""
        out = {}
        for vid in self.definition.active:
            cls = self.definition.variant(vid)
            if cls is not None and cls.AGENT_IMAGE_DIR:
                out.setdefault(self.tag(cls), cls)
        return out

    def ready(self, variant_cls=None, log=print):
        """The images a cell of `variant_cls` runs its agent in, ready: the base
        present (built when missing) with its clients current, then the
        variant's layer over it — every active variant's when `variant_cls` is
        None. False when a build failed; the reason is logged."""
        with mutex.fs_lock(self.lock):
            base = self.base()
            if not _image.present(base):
                log(f"agent image {base} is missing — building it")
                rc = self.rebuild({}, base)
                if rc != 0:
                    log(f"agent image build FAILED (rc={rc})")
                    return False
                log(f"built {base}")
            if not self.ensure_current(base, log):
                return False
            layers = (self.layers().items() if variant_cls is None
                      else [(self.tag(variant_cls), variant_cls)] if variant_cls.AGENT_IMAGE_DIR
                      else [])
            for tag, cls in layers:
                try:
                    for_agent(self.definition, self.conf, cls, self.root, log=log)
                except RuntimeError as e:
                    log(f"agent layer {tag}: {e}")   # a build failure carries the build's tail
                    return False
                log(f"agent image: {tag}")
            return True

    def client_versions(self, image):
        """{cli: version} the image carries. Keyed by image id, so a rebuilt tag
        is probed afresh: one container run per new id, a book read after."""
        iid = image_id(image)
        if not iid:
            return {}
        cache = self.clients_book.load()
        if iid in cache:
            return cache[iid]
        found = self.installed(iid)
        if found:
            cache[iid] = found
            try:
                self.clients_book.save(cache)
            except OSError:
                pass
        return found

    def report(self, img=None):
        """Print installed vs latest per tool, and the layers' tags. Returns the
        stale list."""
        img = img or self.base()
        have, want = self.installed(img), self.upstream(self.arch(img))
        behind = {t for t, _, _ in stale(have, want)}
        for t in TOOLS:
            mark = "BEHIND" if t in behind else ("ok" if have.get(t) else "?")
            print(f"  {t:9} installed {have.get(t, '-'):10} latest {want.get(t, '?'):10} {mark}")
        for layer in self.layers():
            print(f"  layer     {layer:32} {'present' if _image.present(layer) else 'MISSING'}")
        return stale(have, want)
