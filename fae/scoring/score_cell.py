#!/usr/bin/env python3
"""Score ONE experiment cell into workspaces.nosync/<cell_id>/score.json.

Computes the authored surface, iterations-to-green, the verify's results
and the agent's time from the cell's own files. Stdlib only.

Usage:
    python3 fae/scoring/score_cell.py <cell_id>
    python3 fae/scoring/score_cell.py opus_high_x_sealed_apidocs_T1_r1
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from fae.scoring import surface_filter

from fae import paths as _paths  # noqa: E402

REPO_ROOT = _paths.ROOT
# same resolution rule as fae/driver/common.py: the workspace dir was renamed *.nosync to
# keep agent artifacts out of iCloud; scoring hardcoding the old name made
# every score invocation fail "no workspace" (2026-07-24 audit finding 1)
WORKSPACES = next((REPO_ROOT / n for n in ("workspaces.nosync", "workspaces")
                   if (REPO_ROOT / n).is_dir()), REPO_ROOT / "workspaces.nosync")
# (no RESULTS_CELLS: the record moved into the cell's own directory as
# score.json — 9293aa9 — and the old results/cells/ constant lingered unused,
# pointing at a directory nothing writes and no one reads.)

# Extension -> language label (author-surface metric 4).
LANG_BY_EXT = {
    ".py": "Python", ".tf": "HCL", ".hcl": "HCL", ".yaml": "YAML", ".yml": "YAML",
    ".json": "JSON", ".toml": "TOML", ".sh": "Shell", ".sql": "SQL",
    ".md": "Markdown", ".txt": "Text", ".dockerfile": "Dockerfile",
    ".tpl": "Template", ".env": "DotEnv", ".ini": "INI", ".cfg": "INI",
}
# Files whose name (not extension) implies a language.
LANG_BY_NAME = {"Dockerfile": "Dockerfile", "Makefile": "Makefile"}

# Full-line comment leaders per language, for the sloc count. Languages absent
# here (JSON, Text, ...) have no line comments; every non-blank line counts.
_COMMENT_LEADER = {
    "Python": "#", "YAML": "#", "TOML": "#", "Shell": "#", "HCL": "#",
    "Dockerfile": "#", "Makefile": "#", "INI": "#", "DotEnv": "#",
    "SQL": "--", "Template": "#",
}


def _sloc(path: Path, lang: str) -> int:
    """Non-blank lines minus full-line comments. Trailing same-line comments
    still count: the line carries logic."""
    leader = _COMMENT_LEADER.get(lang)
    n = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if leader and s.startswith(leader):
                    continue
                n += 1
    except OSError:
        return 0
    return n


def _factors(vid) -> dict:
    """The factors the variant is a level of, from its file; {} when the
    variant is not (or no longer) one of the experiment's."""
    if not vid:
        return {}
    try:
        for p in (str(_paths.ROOT), str(_paths.ENGINE.parent)):
            if p not in sys.path:
                sys.path.insert(0, p)
        from fae.cell import experiment as _experiment
        cls = _experiment.current().variant(vid)
    except (OSError, RuntimeError, ValueError, ImportError):
        return {}
    return dict(cls.FACTORS) if cls is not None else {}


def _sha256(p: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    try:
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def read_skeleton_manifest(artifacts_dir: Path) -> dict[str, str]:
    """path -> sha256 for every FIXED skeleton file seeded at prepare time.
    Lets us separate the agent-authored surface from the fixed skeleton."""
    manifest = artifacts_dir.parent / ".skeleton_manifest"
    out: dict[str, str] = {}
    if manifest.is_file():
        for line in manifest.read_text().splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                out[parts[0]] = parts[2]
    return out


# Bump when scoring semantics change (sloc rules, surface filter, authored
# logic): a stale cache would otherwise keep serving numbers computed under
# the old rules.
CACHE_VERSION = 1


def load_cache(cell) -> dict:
    try:
        c = json.loads(cell.read_derived("score-cache.json") or "{}")
        return c if c.get("version") == CACHE_VERSION else {}
    except json.JSONDecodeError:
        return {}


def cache_text(cache: dict) -> str:
    cache["version"] = CACHE_VERSION
    return json.dumps(cache)


def cached_input(cache: dict, key: str, mtime_ns, read):
    """Return read()'s result, reusing the cache while its source's mtime_ns
    is unchanged. An absent source caches as (None mtime, read-empty)."""
    ent = cache.get(key)
    if ent is not None and ent.get("mtime_ns") == mtime_ns:
        return ent["data"]
    data = read()
    cache[key] = {"mtime_ns": mtime_ns, "data": data}
    return data


def author_surface(artifacts_dir: Path, cache: dict | None = None) -> dict:
    """Author-surface metric. Reports the TOTAL surface and — using the seeded
    skeleton manifest — the AGENT-AUTHORED surface (files new or modified vs the
    skeleton). The agent-authored numbers are the honest "how much did the
    provisioning slice cost to author" proxy; the total is kept for context."""
    skeleton = read_skeleton_manifest(artifacts_dir)
    # Per-file incremental cache keyed on (mtime_ns, size): only files whose
    # stat changed are re-read/re-hashed; a stat-only sweep decides. The
    # binary sniff inside countable() reads content, so cache hits skip it
    # too — a cached entry implies the file was countable when hashed.
    fcache = (cache or {}).get("surface_files") or {}
    fresh: dict[str, dict] = {}
    files = 0
    langs: set[str] = set()
    total_lines = 0
    a_files = 0
    a_langs: set[str] = set()
    a_lines = 0
    a_sloc = 0
    per_file: list[dict] = []
    if artifacts_dir.is_dir():
        # String-based walk, not sorted(rglob) + Path.relative_to per entry:
        # the pathlib arithmetic was ~70% of a warm sweep's CPU. IGNORE_PARTS
        # subtrees (node_modules, .git, ...) are pruned at the walk — every
        # file under them is un-countable by definition, so descending only
        # to stat and reject each one was pure waste. The sort key mirrors
        # PurePath ordering (tuple of parts) so per_file order is unchanged.
        root_s = str(artifacts_dir)
        rels: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root_s):
            dirnames[:] = [d for d in dirnames
                           if d not in surface_filter.IGNORE_PARTS]
            base = dirpath[len(root_s):].lstrip("/")
            rels.extend(f"{base}/{fn}" if base else fn for fn in filenames)
        rels.sort(key=lambda r: r.split("/"))
        for rel in rels:
            full = f"{root_s}/{rel}"
            ent = fcache.get(rel)
            try:
                st = os.stat(full)
                sig = [st.st_mtime_ns, st.st_size]
            except OSError:
                continue
            if ent is not None and ent["sig"] == sig:
                if ent["skip"]:
                    continue
                row = ent["row"]
                sha = ent["sha"]
                fresh[rel] = ent
            else:
                p = Path(full)
                # ONE definition of authored surface (fae/scoring/surface_filter.py);
                # content-based, so a vendor tree the agent downloads is still caught.
                if not surface_filter.countable(p, artifacts_dir, rel):
                    fresh[rel] = {"sig": sig, "skip": True}
                    continue
                lang = LANG_BY_NAME.get(p.name, LANG_BY_EXT.get(p.suffix.lower(), p.suffix or "other"))
                try:
                    lines = sum(1 for _ in p.open("r", encoding="utf-8", errors="replace"))
                except OSError:
                    lines = 0
                sha = _sha256(p)
                row = {"path": rel, "language": lang, "lines": lines,
                       "sloc": _sloc(p, lang)}
                fresh[rel] = {"sig": sig, "skip": False, "row": row, "sha": sha}
            # authored is derived OUTSIDE the cache entry: it depends on the
            # skeleton manifest, which can change independently of the file.
            authored = rel not in skeleton or skeleton.get(rel) != sha
            lang, lines, sloc = row["language"], row["lines"], row["sloc"]
            files += 1
            langs.add(lang)
            total_lines += lines
            if authored:
                a_files += 1
                a_langs.add(lang)
                a_lines += lines
                a_sloc += sloc
            per_file.append(dict(row, authored=authored))
    if cache is not None:
        cache["surface_files"] = fresh
    return {
        # agent-authored surface (the headline for metric 4)
        "files": a_files,
        "languages": sorted(a_langs),
        "language_count": len(a_langs),
        "lines": a_lines,
        "sloc": a_sloc,
        # total surface (skeleton + authored), for context
        "total_files": files,
        "total_languages": sorted(langs),
        "total_language_count": len(langs),
        "total_lines": total_lines,
        "per_file": per_file,
    }


def parse_iterations(L: dict) -> dict:
    """iterations-to-green from the parsed ledger (fae/cell/ledger.py), the same
    derivation the orchestrator renders and the worker consults: one shared
    derivation keeps a revoked green from scoring as green."""
    green = L["verdict"] == "green"
    if green:
        itg = L["green_at"]
    elif L["iters"]:
        itg = len(L["iters"])
    else:
        itg = None
    return {
        "iterations_to_green": itg if green else None,
        "green": green,
        "revoked": L["verdict"] == "revoked",
        "budget_exhausted": "budget" in L["iters"],
        "iter_sequence": L["iters"],
    }


def parse_agent_time(text: str) -> dict:
    """The agent's wall-clock seconds per charged attempt, from the ledger's
    AGENT lines. A charged attempt is one with an ITER line; a refunded one has
    none and is not counted. An attempt run more than once keeps its last AGENT
    line, the run whose work was judged. The totals are None when a charged
    attempt has no AGENT line (cells that predate it)."""
    charged, green_at, secs = [], None, {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 4:
            continue
        if f[1] == "ITER":
            m = re.search(r"\battempt=(\d+)", f[3])
            if m:
                charged.append(int(m.group(1)))
                if f[2] == "green" and green_at is None:
                    green_at = int(m.group(1))
        elif f[1] == "AGENT":
            kv = dict(x.split("=", 1) for x in f[3:] if "=" in x)
            if kv.get("attempt", "").isdigit() and kv.get("s", "").isdigit():
                secs[int(kv["attempt"])] = int(kv["s"])
    counted = [n for n in charged if green_at is None or n <= green_at]
    complete = bool(counted) and all(n in secs for n in counted)
    return {
        "agent_s_by_attempt": {str(n): secs[n] for n in charged if n in secs},
        "agent_s_total": sum(secs[n] for n in counted) if complete else None,
        "agent_s_per_attempt": (round(sum(secs[n] for n in counted) / len(counted), 1)
                                if complete else None),
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return score_one(sys.argv[1])


def score_one(cell_id: str, cell=None) -> int:
    """Score one cell in-process. fae/driver/score.py's `score()` path-loads this
    module once and calls it per cell so a 300-cell sweep pays
    interpreter+import startup once, not per cell. The record is written
    through the cell (`cell`, else the one in WORKSPACES)."""
    if cell is None:
        from fae.cell.cell import Cell
        cell = Cell(cell_id, workspaces=WORKSPACES, root=REPO_ROOT)
    ws = cell.ws
    if not ws.is_dir():
        print(f"ERROR: no such cell '{cell_id}' at {ws}", file=sys.stderr)
        return 1

    cache = load_cache(cell)
    env = cell.env
    mt = cell.mtimes()
    surface = author_surface(ws / "artifacts", cache)
    iters = cached_input(cache, "iterations", mt["ledger"],
                         lambda: parse_iterations(cell.read_ledger()))
    metrics = cached_input(cache, "metrics", mt["metrics"], cell.read_metrics)
    agent_time = cached_input(cache, "agent_time", mt["ledger"],
                              lambda: parse_agent_time(cell.ledger_text()))

    itg = iters["iterations_to_green"]
    
    record = {
        "cell_id": cell_id,
        "model": env.get("AGENT_MODEL"),
        "task": env.get("TASK"),
        "variant": env.get("VARIANT"),
        "factors": _factors(env.get("VARIANT")),
        # Which cell driver ran it: "py" from the rig cutover on, "bash"
        # before (those cell.env files carry IMPL=bash or no IMPL at all).
        "impl": env.get("IMPL") or "bash",
        "repeat": int(env.get("REPEAT", "1")) if env.get("REPEAT", "1").isdigit() else env.get("REPEAT"),
        "attempt_budget": int(env["ATTEMPT_BUDGET"]) if env.get("ATTEMPT_BUDGET", "").isdigit() else None,
        
        # metric 2b (AUTO — verify.sh live functional + load result)
        "deploy_ok": metrics.get("deploy_ok"),
        "e2e_pass": metrics.get("e2e_pass"),
        "e2e_total": metrics.get("e2e_total"),
        "e2e_green": metrics.get("e2e_green"),
        "load_errors": metrics.get("load_errors"),
        "load_total": metrics.get("load_total"),
        "load_ran": metrics.get("load_ran"),
        "k6_available": metrics.get("k6_available"),
        "verify_stage_failed": metrics.get("stage_failed"),
        
        # metric 3
        "iterations_to_green": itg,
        "green": iters["green"],
        "revoked": iters["revoked"],
        "budget_exhausted": iters["budget_exhausted"],

        # authoring time: the agent's wall-clock, charged attempts up to green
        "agent_s_total": agent_time["agent_s_total"],
        "agent_s_per_attempt": agent_time["agent_s_per_attempt"],
        "agent_s_by_attempt": agent_time["agent_s_by_attempt"],
        
        # metric 4
        "author_surface": surface,
    }

    with cell.changing():
        cell.write_derived("score.json", json.dumps(record, indent=2) + "\n")
        try:
            cell.write_derived("score-cache.json", cache_text(cache))
        except OSError:
            pass                    # the cache only saves work; scoring stands without it
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
