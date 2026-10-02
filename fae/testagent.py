#!/usr/bin/env python3
"""The test agent: a scripted coding agent that also audits its own invocation.

Runs as AGENT=testagent, in the same container, with the same mounts, started
the same way as every other agent — so a bug in how the harness starts an agent
surfaces here instead of after burning real tokens on a real one.

WHAT IT SEES, and why that is the point. A real agent's container has exactly
two mounts: the cell's artifacts at /workspace, and its own home. This agent
gets the same two — its home carries this script and the reference
implementation instead of a credential — so an audit finding of "I can see the
harness" is a genuine isolation failure and not an artefact of the test setup.

TWO JOBS.

  1. AUDIT. Everything the harness handed it — the prompt TEXT (agents are
     given the prompt as an argument, not a path), the working directory, the
     environment, the reachable filesystem — is recorded in findings.json in
     its home, one entry per invocation. A harness that starts the agent
     wrongly becomes a readable fact rather than a mystery verdict three
     stages later.

  2. ACT, to a script. TESTAGENT_PLAN names what to do on each attempt, so the
     attempt LOOP is reproducible: fail twice then solve, edit nothing, touch a
     fixed file, die silently. Its solution is the committed reference
     implementation, so a "green" plan really does green.

PLAN GRAMMAR — comma-separated steps, one per attempt; the last one repeats.

    green            solve now, with the reference implementation
    green:N          fail until attempt N, then solve on it
    fail             never solve — drives the cell to budget exhaustion
    noedit           write nothing at all (the no-edit guard)
    heal             edit a FIXED file (restore-and-judge reverts it)
    crash            exit non-zero without producing a build
    silent           produce no output at all (the agent-fault retry path)
    limit            print a usage-limit line (the transient-fault path)
    limit429         print the Claude CLI's stream-json weekly-limit result
                     (api_error_status 429) and exit 0 — the wall that exits
                     clean and edits nothing
    auth             print a login-required line and exit 0 (the auth wall:
                     halts the cell, never retried)
    slow:S           sleep S seconds, then solve (phase age, supervision)

    TESTAGENT_PLAN=fail,fail,green   fails twice, greens on the third attempt
    TESTAGENT_PLAN=green:3           the same, spelled by target attempt
    TESTAGENT_PLAN=fail              never greens; the cell spends its budget
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

# What real CLIs print when the account is walled. fae/cell/faults.py
# must classify each, or the fault paths cannot be exercised.
LIMIT_LINE = "API Error: Claude usage limit reached. Resets in 3 hours"
AUTH_LINE = "Authentication required. Please visit the URL to log in:"
# Verbatim from a claude-sonnet-5 cell on 2026-08-25 (session ids kept): the
# CLI reports the wall as a successful run whose result is an error.
LIMIT429_LINES = (
    '{"type":"rate_limit_event","rate_limit_info":{"status":"rejected",'
    '"resetsAt":1787702400,"rateLimitType":"seven_day","overageStatus":'
    '"rejected","overageDisabledReason":"org_level_disabled","isUsingOverage":'
    'false},"uuid":"a372e51c-3193-40fb-b603-fb8bf49c9b06","session_id":'
    '"b8e9dfe1-3c9f-4a32-9e67-43e167d46fb1"}',
    '{"type":"assistant","message":{"id":"5f993a3f-27b8-4d23-b22b-84e4423cbdcd",'
    '"container":null,"model":"<synthetic>","role":"assistant","stop_details":'
    'null,"stop_reason":"stop_sequence","stop_sequence":"","type":"message",'
    '"usage":{"input_tokens":0,"output_tokens":0,"cache_creation_input_tokens":'
    '0,"cache_read_input_tokens":0,"server_tool_use":{"web_search_requests":0,'
    '"web_fetch_requests":0},"service_tier":null,"cache_creation":'
    '{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":0},'
    '"inference_geo":null,"iterations":null,"speed":null},"content":[{"type":'
    '"text","text":"You\'ve hit your weekly limit \u00b7 resets 12am (UTC)"}],'
    '"context_management":null},"parent_tool_use_id":null,"session_id":'
    '"b8e9dfe1-3c9f-4a32-9e67-43e167d46fb1","uuid":'
    '"cabd79a2-21be-44cc-994c-023f9adcf97e","error":"rate_limit","request_id":'
    '"req_011CeQ8gi5Bezk6JqvPhgY5g"}',
    '{"type":"result","subtype":"success","is_error":true,"api_error_status":'
    '429,"duration_ms":599,"duration_api_ms":0,"num_turns":1,"result":"You\'ve '
    'hit your weekly limit \u00b7 resets 12am (UTC)","stop_reason":'
    '"stop_sequence","session_id":"b8e9dfe1-3c9f-4a32-9e67-43e167d46fb1",'
    '"total_cost_usd":0,"usage":{"input_tokens":0,"cache_creation_input_tokens":'
    '0,"cache_read_input_tokens":0,"output_tokens":0,"server_tool_use":'
    '{"web_search_requests":0,"web_fetch_requests":0},"service_tier":"standard",'
    '"cache_creation":{"ephemeral_1h_input_tokens":0,"ephemeral_5m_input_tokens":'
    '0},"inference_geo":"","iterations":[],"speed":"standard"},"modelUsage":{},'
    '"permission_denials":[],"terminal_reason":"completed","fast_mode_state":'
    '"off","uuid":"23af5e50-2926-4b2a-98a0-2b4083228284"}',
)

HOME = Path(os.environ.get("TESTAGENT_HOME", "/home/node/.testagent"))
FINDINGS = HOME / "findings.json"
REFERENCE = HOME / "reference"


def audit(prompt_text):
    """What the harness handed this invocation."""
    cwd = Path.cwd()
    return {
        "cwd": str(cwd),
        "cwd_is_workspace": cwd.name == "workspace",
        "cell_id": os.environ.get("CELL_ID", ""),
        # Agents receive the prompt as an ARGUMENT; a path would be a
        # host path this container cannot resolve.
        "prompt_len": len(prompt_text),
        "prompt_names_task": "TODO.md" in prompt_text,
        "has_feedback": "BUILD FEEDBACK" in prompt_text,
        "names_stage": "stage:" in prompt_text,
        "names_arrangement": "FAILED on arrangement" in prompt_text,
        "says_no_edit": "modified NO files" in prompt_text,
        "says_heal": "were reverted before verification" in prompt_text,
        "has_verify_tail": "## verify.log (tail)" in prompt_text,
        # The seeded surface it must be able to author against.
        "sees_todo": (cwd / "TODO.md").is_file(),
        "sees_docs": (cwd / "docs").is_dir(),
        "sees_app": (cwd / "app").is_dir(),
        "app_writable": os.access(cwd / "app", os.W_OK) if (cwd / "app").is_dir() else False,
        "todo_writable": os.access(cwd / "TODO.md", os.W_OK) if (cwd / "TODO.md").is_file() else None,
        # Isolation: none of these may be reachable from inside the container.
        "sees_harness": (cwd / "fae").exists() or Path("/harness").exists(),
        "sees_instruments": Path("/instruments").exists(),
        "sees_workspaces": Path("/workspaces").exists(),
        "sees_repo_root": Path("/runs.py").exists(),
        "has_own_home": HOME.is_dir(),
    }


def record(entry):
    """Append one invocation. The index IS the attempt number, since the
    harness invokes the agent once per attempt."""
    try:
        seen = json.loads(FINDINGS.read_text())
    except (OSError, ValueError):
        seen = []
    entry["attempt"] = len(seen) + 1
    seen.append(entry)
    FINDINGS.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS.write_text(json.dumps(seen, indent=2) + "\n")
    return entry["attempt"]


def solve(attempt):
    """Write the committed answer for this cell's variant (VARIANT in the
    environment, set by the driver)."""
    variant = os.environ.get("VARIANT")
    if not variant:
        print("testagent: VARIANT not in the environment", file=sys.stderr)
        return 1
    ref = REFERENCE / variant
    if not ref.is_dir():
        print(f"testagent: no reference implementation at {ref}", file=sys.stderr)
        return 1
    stub = Path.cwd() / "app" / "testagent_stub.py"
    if stub.exists():
        stub.unlink()                    # an earlier attempt's unsolved marker
    for p in sorted(ref.rglob("*")):
        dst = Path.cwd() / p.relative_to(ref)
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.chmod(0o644)
        shutil.copy2(p, dst)
    print(f"testagent: attempt {attempt} wrote the reference implementation")
    return 0


def unsolved(attempt):
    """A build that still deploys and serves, but does not satisfy the law."""
    (Path.cwd() / "app" / "testagent_stub.py").write_text(
        f"# testagent attempt {attempt}: deliberately not a solution\n"
        f"MOUNTS = False\n")
    print(f"testagent: attempt {attempt} left the task unsolved")
    return 0


# Non-authorable AND seeded from the skeleton, so restore_fixed has a source to
# restore from. TODO.md and docs/ are seeded from the task materials instead,
# and an edit to those is caught later by the verify's authorable check.
FIXED_FILE = "README.md"


def heal(attempt):
    """Edit outside the authorable surface. restore-and-judge reverts it, and
    the next prompt must say so."""
    f = Path.cwd() / FIXED_FILE
    try:
        f.chmod(0o644)
        f.write_text(f.read_text() + f"\n<!-- testagent {attempt} -->\n")
        print(f"testagent: attempt {attempt} edited the FIXED file {FIXED_FILE}")
    except OSError as e:
        print(f"testagent: could not edit {FIXED_FILE}: {e}", file=sys.stderr)
    # An attempt spent editing a fixed file has not solved the task.
    return unsolved(attempt)


def step_for(plan, attempt):
    """The step for this attempt; the last one repeats for every attempt after."""
    steps = [s.strip() for s in plan.split(",") if s.strip()]
    if len(steps) == 1 and steps[0].startswith("green:"):
        return "green" if attempt >= int(steps[0].split(":", 1)[1]) else "fail"
    return steps[min(attempt, len(steps)) - 1]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    prompt = argv[0] if argv else ""
    attempt = record(audit(prompt))
    step = step_for(os.environ.get("TESTAGENT_PLAN", "green"), attempt)

    if step.startswith("slow:"):
        time.sleep(float(step.split(":", 1)[1]))
        step = "green"
    if step == "silent":
        return 0
    if step == "limit":
        print(LIMIT_LINE, file=sys.stderr)
        return 1
    if step == "limit429":
        for line in LIMIT429_LINES:
            print(line)
        return 0
    if step == "auth":
        print(AUTH_LINE)
        return 0
    if step == "crash":
        print("testagent: deliberate crash", file=sys.stderr)
        return 2
    if step == "noedit":
        print(f"testagent: attempt {attempt} edited nothing")
        return 0
    if step == "heal":
        return heal(attempt)
    if step == "green":
        return solve(attempt)
    return unsolved(attempt)


if __name__ == "__main__":
    sys.exit(main())
