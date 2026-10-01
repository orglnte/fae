#!/usr/bin/env python3
"""Score ONE experiment cell into workspaces.nosync/<cell_id>/score.json.

Auto-computes author-surface + iterations-to-green + doc_lines from the cell;
merges the human-graded fields from the cell's grading.json (fill it from
fae/scoring/grading-template.json). Stdlib only — no install needed.

Usage:
    python3 fae/scoring/score_cell.py <cell_id>
    python3 fae/scoring/score_cell.py opus_high_x_sealed_apidocs_T1_r1
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from fae import metrics as _metrics
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


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


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
    manifest = artifacts_dir / ".skeleton_manifest"
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


def _mtime_ns(p: Path):
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return None


def load_cache(ws: Path) -> dict:
    try:
        c = json.loads((ws / "score-cache.json").read_text())
        return c if c.get("version") == CACHE_VERSION else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(ws: Path, cache: dict) -> None:
    cache["version"] = CACHE_VERSION
    try:
        (ws / "score-cache.json").write_text(json.dumps(cache))
    except OSError:
        pass


def cached_input(cache: dict, key: str, path: Path, parse):
    """Return parse(path)'s result, reusing the cache when the file's
    mtime_ns is unchanged. Absent files cache as (None mtime, parsed-empty)."""
    mt = _mtime_ns(path)
    ent = cache.get(key)
    if ent is not None and ent.get("mtime_ns") == mt:
        return ent["data"]
    data = parse(path)
    cache[key] = {"mtime_ns": mt, "data": data}
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
                # ONE definition of authored surface, shared with
                # count_defects.py (fae/scoring/surface_filter.py); content-based,
                # so a vendor tree the agent downloads is still caught.
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


def read_metrics(ws: Path) -> dict:
    """verify.sh output (metrics.json): e2e_pass/e2e_total, load_errors/
    load_total, deploy_ok, e2e_green, stage_failed. Auto-measured — not
    human-graded."""
    return _metrics.read(ws)


def parse_iterations(log_path: Path) -> dict:
    """iterations-to-green — derived by THE ledger library (fae/ledger.py),
    the same derivation the orchestrator renders and the worker consults. This module
    used to carry its own parser and scored revoked greens as green (audit
    finding 2); one shared derivation makes that class of divergence
    impossible."""
    from fae import ledger
    L = ledger.parse(log_path.parent)
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


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return score_one(sys.argv[1])


def score_one(cell_id: str) -> int:
    """Score one cell in-process. fae/driver/score.py's `score()` path-loads this
    module once and calls it per cell so a 300-cell sweep pays
    interpreter+import startup once, not per cell."""
    ws = WORKSPACES / cell_id
    if not ws.is_dir():
        print(f"ERROR: no such cell '{cell_id}' at {ws}", file=sys.stderr)
        return 1

    cache = load_cache(ws)
    env = cached_input(cache, "env", ws / "cell.env", read_env)
    surface = author_surface(ws / "artifacts", cache)
    iters = cached_input(cache, "iterations", ws / "iterations.log",
                         parse_iterations)
    metrics = cached_input(cache, "metrics", ws / "metrics.json",
                           lambda _p: read_metrics(ws))

    # LLM Judge (defects.json)
    defects_path = ws / "defects.json"
    defects_missing = not defects_path.is_file()
    defects_data: dict = {}
    if not defects_missing:
        def _parse_defects(p):
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                return None
        defects_data = cached_input(cache, "defects", defects_path, _parse_defects)
        if defects_data is None:
            print(f"ERROR: {defects_path} is not valid JSON", file=sys.stderr)
            return 1

    if defects_missing:
        consistency_defect_count = None
        codes = []
    else:
        codes = defects_data.get("codes", []) or []
        # THE primary metric. Each entry is an OBJECT — {"attempt":…,
        # "code":"CAD5", "artifacts":[…], "description":…} from the LLM judge,
        # and the same shape in grading-template.json's defects[] for a human
        # grader. This used to be `str(code).startswith("CAD")`, which
        # stringifies the whole dict to "{'attempt': None, 'code': 'CAD5'…}" —
        # always starting with "{", so the count was structurally pinned to 0.
        # It went unseen because no defects.json existed until 2026-07-29.
        # Tolerate a bare string entry too, in case a hand-written file uses one.
        consistency_defect_count = sum(
            1 for c in codes
            if str(c.get("code", "") if isinstance(c, dict) else c).startswith("CAD"))

    itg = iters["iterations_to_green"]
    
    record = {
        "cell_id": cell_id,
        "model": env.get("MODEL_VERSION"),
        "task": env.get("TASK"),
        "treatment": env.get("TREATMENT"),
        "condition": env.get("CONDITION"),
        # Which cell driver ran it: "py" from the rig cutover on, "bash"
        # before (those cell.env files carry IMPL=bash or no IMPL at all).
        "impl": env.get("IMPL") or "bash",
        "repeat": int(env.get("REPEAT", "1")) if env.get("REPEAT", "1").isdigit() else env.get("REPEAT"),
        "doc_lines": int(env["DOC_LINES"]) if env.get("DOC_LINES", "").isdigit() else None,
        "attempt_budget": int(env["ATTEMPT_BUDGET"]) if env.get("ATTEMPT_BUDGET", "").isdigit() else None,
        
        # metric 1: LLM Judge (defects.json)
        "consistency_defect_count": consistency_defect_count,
        "codes": codes,
        
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
        
        # metric 4
        "author_surface": surface,
        
        # provenance
        "grader_model": defects_data.get("grader_model"),
    }

    out = ws / "score.json"
    out_text = json.dumps(record, indent=2)
    out.write_text(out_text + "\n")
    save_cache(ws, cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
