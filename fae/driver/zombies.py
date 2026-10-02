"""Orphaned rig resources: zombie containers/clusters/loops the fleet no
longer owns, and the reaper's owner-protection logic that must never touch a
live cell.

_VERIFY_HOLDER_ARGV / _is_driver_pid answer "is this pid entitled to hold the
lock it has" for a verify running via `cli.py rig exp1|cell reverify|rig
smoke` in the FOREGROUND process — the regex tracks the real invoked argv,
so moving the code that implements a verb changes nothing about what it
matches; only a change to the invocation shape itself does. Substrate names
(the dind sidecar, the kind cluster) come from the variants that provision
them (Variant.substrate_identities, SUBSTRATE_PREFIXES for what a scan may
discover, stray() for what carries no name at all): the reaper keeps no
formula of its own, so a live cell's cluster cannot lose its owner to a
divergent copy and be deleted out from under an agent.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fae.driver import common, state
from fae.driver.common import parse_cell_id, awake_age, mutex

ZOMBIE_GRACE_S = int(os.environ.get("ZOMBIE_GRACE_S", 600))      # owned, recent


ZOMBIE_UNKNOWN_GRACE_S = int(os.environ.get("ZOMBIE_UNKNOWN_GRACE_S", 3600))

_CLMAP = {"key": None, "map": {}}


def _workspace_entries():
    """The workspace root's entries; none when the root does not exist yet
    (a fresh checkout, a root no cell has run in)."""
    try:
        return list(common.WS.iterdir())
    except OSError:
        return []


def _cluster_map():
    """cid -> the cluster a cell of that cid owns, as its variant names it
    (`substrate_identities`), cached per cid-set: a pure function of the cid,
    computed in-process — an owner map that could come back empty on a
    fault would strip every live cluster of its reaper protection."""
    cids = tuple(sorted(p.name for p in _workspace_entries()
                        if p.is_dir() and parse_cell_id(p.name)))
    if _CLMAP["key"] != cids:
        _CLMAP["key"] = cids
        m = {}
        for c in cids:
            tech = common.definition().tech_of(parse_cell_id(c)[1])
            for kind, ident in _substrate_of(tech, c):
                if kind == "cluster":
                    m[c] = ident
        _CLMAP["map"] = m
    return _CLMAP["map"]
def _substrate_of(tech, cid):
    """[(kind, ident)] a cell of this tech provisions, named by its variants.
    Unnamed substrate is found by scanning instead (`_strays`)."""
    from fae.cell import variants as _tr
    out = []
    for cls in _tr.registry().values():
        if cls.TECH == tech:
            for pair in cls.substrate_identities(cid):
                if pair not in out:
                    out.append(pair)
    return out


def _verifier_substrate(cid):
    """[(kind, ident)] the engine and the experiment's verifier provision
    for a cell of any tech: the verify container and the cell network, then
    the verifier's own (a store), named as they name them."""
    from fae.cell import image as _image
    out = [("container", _image.verify_container(cid)), ("network", _image.cell_network(cid))]
    for pair in common.definition().verifier_class().substrate_identities(cid):
        if pair not in out:
            out.append(pair)
    return out


def _containers_all():
    """Every container name, running or not. containers() is `docker ps` and
    its callers mean "running"; a STOPPED sidecar still owns its name."""
    return set(common.sh(["docker", "ps", "-a", "--format", "{{.Names}}"]).split())


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def _strays(live):
    """Substrate only a scan can find, from every variant: [(kind, ident,
    owner cid)] whose owner has no live loop (Variant.stray)."""
    from fae.cell import variants as _tr
    out = []
    for cls in _tr.registry().values():
        for item in cls.stray(live, common.WS):
            if item not in out:
                out.append(item)
    return out


def _prefixes():
    """[(kind, prefix)] a reaper scans by: the engine's agent container,
    each variant's SUBSTRATE_PREFIXES and the verifier's."""
    from fae.cell import image as _image
    from fae.cell import variants as _tr
    out = [("container", common.AGENT_CONTAINER_PREFIX),
           ("container", _image.VERIFY_PREFIX), ("container", _image.TOOL_PREFIX),
           ("container", _image.RUN_PREFIX),
           ("network", _image.NET_PREFIX)]
    classes = list(_tr.registry().values()) + [common.definition().verifier_class()]
    for cls in classes:
        for kind, pfxs in cls.SUBSTRATE_PREFIXES.items():
            for pfx in ((pfxs,) if isinstance(pfxs, str) else pfxs):
                if (kind, pfx) not in out:
                    out.append((kind, pfx))
    return out


def _docker_net_age_s(name):
    out = common.sh(["docker", "network", "inspect", "-f", "{{.Created}}", name]).strip()
    try:
        return time.time() - datetime.fromisoformat(out.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _kill_process(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return
    for _ in range(10):
        if not _pid_alive(pid):
            return
        time.sleep(1)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass


def _docker_age_s(name):
    out = common.sh(["docker", "inspect", "-f", "{{.Created}}", name]).strip()
    try:
        return time.time() - datetime.fromisoformat(
            out.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iter_age_s(cid):
    try:
        return awake_age((common.WS / cid / "iterations.log").stat().st_mtime)
    except OSError:
        return None


def _fd_holders(path):
    """pids with `path` open. Not the flock holder — every driver opens all
    of its candidates — so this is only meaningful once the legitimate holder
    is known to be gone."""
    out = common.sh(["lsof", "-t", "-n", str(path)])
    return [int(x) for x in out.split() if x.strip().isdigit()]
# The processes entitled to hold the rig lock or the verify lock: a cell loop
# (found by loop_parents(), default root only) and the out-of-loop verbs that
# run a verify through the cell — `exp1` (the reference benchmark, in
# smoke-workspaces), `reverify` (under <ws>/reverify/<ts>/), and `smoke`
# (whose stub cells live in ws-test.nosync, invisible to loop_parents).
# cli.py's noun-grouped invocation shape (`cli.py rig exp1`, `cli.py cell
# reverify`, `cli.py rig smoke` — not a flat `cli\.py\s+(exp1|reverify|
# smoke)`, since the group token differs per verb). Used to also match
# `runs.py exp1|reverify|smoke` as an OR while runs.py stayed invocable
# (Milestone 5); dropped at Milestone 6 when runs.py was deleted — no
# process can be invoked that way any more.


_VERIFY_HOLDER_ARGV = r"cli\.py\s+(rig\s+exp1|cell\s+reverify|rig\s+smoke)\b"


def _verify_holders_alive():
    return bool(common.sh(["pgrep", "-f", _VERIFY_HOLDER_ARGV]).split())


def _leaked_lock_holders():
    """[(lockname, pid)] for a lock that is HELD while the process entitled to
    hold it does not exist.

    Both locks are held IN-PROCESS, as a file object: the rig lock by
    Cell.exclusive_acquire, the verify lock by Cell.verify_lock_acquire —
    inside a cell loop, or inside `cli.py rig exp1|cell reverify|rig smoke`. Either held
    with no such process alive means something else inherited the fd and
    never let go, which blocks the next verify indefinitely — the holder
    cannot be identified while a legitimate one is running, so the absence is
    what makes the remaining fd holders nameable.

    The predicate must name EVERY holder shape: one that knows only the loops
    reads a live exp1 verify as a leak and reaps it mid-run.
    """
    out = []
    entitled = {
        "rig-lock": lambda: bool(state.loop_parents()) or _verify_holders_alive(),
        "verify-lock": lambda: bool(state.loop_parents()) or _verify_holders_alive(),
    }
    for name, is_entitled_alive in entitled.items():
        p = common.ORCH / name
        if not p.exists() or not mutex.probe_held(p) or is_entitled_alive():
            continue
        for pid in _fd_holders(p):
            if _pid_alive(pid) and not _is_driver_pid(pid):
                out.append((name, pid))
    return out


def _is_driver_pid(pid):
    """The holder itself is a cell driver — the python cell (`-m fae.cell`)
    or a cli.py verb that verifies in-process — wherever its workspace
    lives. loop_parents() sees only the default root, so a cell run by hand
    in a validation root (ws-test.nosync) is invisible to it; the lock it
    holds is not a leak."""
    cmd = common.sh(["ps", "-o", "command=", "-p", str(pid)])
    return "fae.cell" in cmd or bool(re.search(_VERIFY_HOLDER_ARGV, cmd))


def find_zombies():
    """[(kind, ident, owner, note)] — resources with no live owner, past
    grace. Read-only; reap_zombies() acts."""
    live = state.loop_parents()
    zs = []
    for lock, pid in _leaked_lock_holders():
        zs.append(("lockholder", f"{pid}:{lock}", mutex.holder_name(common.ORCH / lock) or "?",
                   f"holds {lock} with no process entitled to it"))
    prefixes = _prefixes()
    for name in state.containers():
        for pfx in [p for k, p in prefixes if k == "container"]:
            if not name.startswith(pfx):
                continue
            cid = name[len(pfx):]
            if cid in live:
                continue
            age = _iter_age_s(cid)
            # UNKNOWN age gets the LONG grace, never none: _iter_age_s is None
            # for any cid without an iterations.log under WS — the normal case
            # for exp1/smoke cells, which live under smoke-workspaces.nosync —
            # and `zombies --reap` acts on a single sighting, so no grace here
            # means deleting a running benchmark's sidecar mid-measurement.
            grace = ZOMBIE_GRACE_S if age is not None else ZOMBIE_UNKNOWN_GRACE_S
            box_age = _docker_age_s(name)
            eff = age if age is not None else box_age
            if eff is not None and eff < grace:
                continue          # just finished/mid-teardown, or not ours
            if eff is None:
                continue          # nothing establishes its age: leave it alone
            zs.append(("container", name, cid, "no live loop"))
    by_cluster = {v: k for k, v in _cluster_map().items()}
    cluster_prefixes = tuple(p for k, p in prefixes if k == "cluster")
    for cl in common.sh(["kind", "get", "clusters"]).split():
        if not cluster_prefixes or not cl.startswith(cluster_prefixes):
            continue
        owner = by_cluster.get(cl)
        if owner and owner in live:
            continue              # a live cell's cluster (incl. per-verify)
        age = _docker_age_s(f"{cl}-control-plane")
        grace = ZOMBIE_GRACE_S if owner else ZOMBIE_UNKNOWN_GRACE_S
        if age is None:
            continue              # age unknown => conservative: never reap
        if age < grace:
            continue              # young; smoke/exp1 verifies own unmapped
                                  # clusters briefly — long grace covers them
        zs.append(("cluster", cl, owner or "?", "no live owner"))
    net_prefixes = tuple(p for k, p in prefixes if k == "network")
    for net in (common.sh(["docker", "network", "ls", "--format", "{{.Name}}"]).split()
                if net_prefixes else []):
        if not net.startswith(net_prefixes):
            continue
        pfx = next(p for p in net_prefixes if net.startswith(p))
        cid = net[len(pfx):]
        if cid in live:
            continue
        age = _docker_net_age_s(net)
        if age is None or age < ZOMBIE_UNKNOWN_GRACE_S:
            continue              # young or unknown: a verify may be between
                                  # creating it and starting its store
        zs.append(("network", net, cid, "no live loop"))
    for pid, cid in state.loop_pids().items():
        try:
            ppid = int(common.sh(["ps", "-o", "ppid=", "-p", str(pid)]).strip() or 0)
        except ValueError:
            continue
        if ppid == 1:
            zs.append(("tee", str(pid), cid, "orphaned logger (ppid 1)"))
    for ws in _workspace_entries():
        if ws.is_dir() and (ws / ".loop").exists() and state.heartbeat(ws) is None:
            zs.append(("heartbeat", str(ws / ".loop"), ws.name, "corpse file"))
    # No stale-lock class: a lock is held by fd, so the kernel frees it when
    # its holder dies. What can outlive a holder is SUBSTRATE, and an arm
    # slot's last-holder sidecar is the cheapest place to notice it.
    present = None                      # substrate that exists, read once
    for d in sorted(common.ORCH.glob("arm-*.slots/slot-*")):
        if d.name.endswith(".holder"):
            continue
        owner = mutex.holder_name(d)
        if not owner or owner in live or state._lock_is_held(d):
            continue
        if present is None:
            present = _containers_all() | set(common.sh(["kind", "get", "clusters"]).split())
        arm = d.parent.name[len("arm-"):-len(".slots")]
        named = [(k, i) for k, i in _substrate_of(arm, owner) + _verifier_substrate(owner)
                 if i in present]
        if not named:
            # The sidecar outlived everything it named: nothing left to reap,
            # and keeping the note would re-report the same phantom every tick.
            mutex.clear_holder(d)
            continue
        for kind, ident in named:
            zs.append((kind, ident, owner, f"arm slot's last holder is gone"))
    for kind, ident, cid in _strays(live):
        age = _iter_age_s(cid)
        grace = ZOMBIE_GRACE_S if age is not None else ZOMBIE_UNKNOWN_GRACE_S
        if age is not None and age >= grace:
            zs.append((kind, ident, cid, "no live loop"))
    return zs


def reap_zombies(zs):
    """Act on find_zombies() output. Returns human lines of what was done.

    find_zombies gathers, then this acts — and the gap between them is long:
    the actions ahead allow up to 180s for a cluster delete and 60s for a
    container removal, so the world can change underneath a finding. Every
    branch that ends a process therefore re-confirms its target first.
    """
    done = []
    for kind, ident, owner, _note in zs:
        try:
            if kind == "container":
                subprocess.run(["docker", "rm", "-f", "-v", ident],
                               capture_output=True, timeout=60)
            elif kind == "cluster":
                subprocess.run(["kind", "delete", "cluster", "--name", ident],
                               capture_output=True, timeout=180)
            elif kind == "network":
                subprocess.run(["docker", "network", "rm", ident],
                               capture_output=True, timeout=60)
            elif kind == "tee":
                os.kill(int(ident), signal.SIGTERM)
            elif kind == "heartbeat":
                # Re-confirm the corpse: a new loop may have written a fresh
                # .loop in the interval, and deleting a LIVE heartbeat costs
                # the phase/WAITING display for the rest of the attempt.
                if state.heartbeat(Path(ident).parent) is not None:
                    done.append(f"skipped heartbeat {ident} — beating again")
                    continue
                os.unlink(ident)
            elif kind == "process":
                # Re-confirm at reap time: gathering and acting are minutes
                # apart, and a pid can be recycled or the cell resumed since.
                pid_s, _, cid = ident.partition(":")
                if (kind, ident, cid) not in _strays(state.loop_parents()):
                    done.append(f"skipped process {ident} — live again or "
                                f"pid recycled")
                    continue
                _kill_process(int(pid_s))
            elif kind == "lockholder":
                # Re-confirm: a legitimate holder starting between the sighting
                # and now makes the remaining fd holders unnameable again.
                pid_s, _, lock = ident.partition(":")
                if (lock, int(pid_s)) not in _leaked_lock_holders():
                    done.append(f"skipped lockholder {ident} — entitled again")
                    continue
                _kill_process(int(pid_s))     # TERM, grace, KILL
            done.append(f"reaped {kind} {ident} ({owner})")
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            done.append(f"FAILED reaping {kind} {ident}: {e}")
    return done


def janitor_lines():
    """Two-stage-delete review: list .to_be_deleted entries older than 24 h.
    NEVER deletes anything — deletion happens only after the operator reviews
    and explicitly confirms (SAFE DATA DELETION rule)."""
    out = []
    for root in (common.WS, common.SMOKE_WS):
        tbd = root / ".to_be_deleted"
        if not tbd.is_dir():
            continue
        for entry in sorted(p for p in tbd.iterdir() if p.is_dir()):
            age_h = (time.time() - entry.stat().st_mtime) / 3600
            if age_h > 24:
                out.append(f"  janitor: {entry.relative_to(common.ROOT)} is {age_h:.0f}h old "
                           f"— review + confirm with operator before deleting")
    return out
