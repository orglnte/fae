"""A throwaway container of an image over one directory: how a verifier
runs a judged program without it ever touching the host.

`run` starts `docker run --rm` of `image` with `workdir` mounted at
/workspace, no network, a memory and pid ceiling, the given argv and
stdin; it returns what the program answered. A timeout removes the
container, since the client's death alone does not. `fresh_copy` is the
verifier's own copy of the artifacts, so a build writes there and never
into the judged tree.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

MEMORY = "256m"
PIDS = 128


def argv(image, name, workdir, program, mounts=(), memory=MEMORY, pids=PIDS):
    """The `docker run` argv: `program` inside `image`, `workdir` at
    /workspace, `mounts` as (host path, container path) read-only extras."""
    extra = []
    for host, inner in mounts:
        extra += ["-v", f"{host}:{inner}:ro"]
    return (["docker", "run", "--rm", "-i", "--name", name,
             "--network", "none", "--memory", memory, "--pids-limit", str(pids),
             "-v", f"{workdir}:/workspace", "-w", "/workspace"]
            + extra + [image] + list(program))


def remove(name):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def run(image, name, workdir, program, stdin="", timeout_s=20, mounts=(), env=None):
    """(stdout, stderr, rc, error) of one run; `error` names a rig fault
    (a timeout, no docker client), rc and stderr are the program's."""
    try:
        p = subprocess.run(argv(image, name, workdir, program, mounts), input=stdin,
                           capture_output=True, text=True, timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        remove(name)
        return "", "", -1, f"timed out after {timeout_s}s"
    except OSError as e:
        return "", "", -1, str(e)
    return p.stdout, p.stderr, p.returncode, None


def fresh_copy(artifacts, out, name="run"):
    """`<out>/<name>`, a copy of `artifacts` rebuilt for every verify."""
    workdir = Path(out) / name
    if workdir.is_dir():
        shutil.rmtree(workdir)
    shutil.copytree(artifacts, workdir)
    return workdir
