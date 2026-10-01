#!/usr/bin/env python3
"""Generate a discriminate-profile stage list — any arrangement of Blip and
Spike events — and write the schedule to <ws>/schedule.json.

Usage: load_shape.py <workspace_dir>
Env:   VERIFY_SHAPE       explicit arrangement: a string of B/S with >=1 S
                          (e.g. BBS, BSB, SBB, SSB, SBS, BSS)
       VERIFY_SHAPE_SEED  int; round-robin over the curated set (used when
                          VERIFY_SHAPE is unset)
       DISC_T_BASELINE / DISC_T_RAMP / DISC_T_SPIKE / DISC_T_TAIL
                          stage durations (s)

A spike is a RAMP from the baseline to the top rate over DISC_T_RAMP, then
a hold at the top for DISC_T_SPIKE: the ramp crosses the store's knee
wherever a host puts it, and the law measures from that crossing, not from
here. A blip is the same ramp with a stop rule, driven by a SEPARATE load
generator the verifier starts at the window's t0 (it aborts at the first
sign of pressure); in these stages the window is baseline, its length the
ramp plus a margin.

Multi-spike arrangements test RE-ACQUISITION (mount, release, mount again on
the next genuine spike) — the one-shot-policy blind spot. For them the spike
hold defaults to 25s and inter-event baselines to 15s (release lag needs the
recovery gap; both references release within ~10s of the drain), unless the
DISC_T_* envs are set explicitly.

Prints the k6 stage list (JSON) on stdout; the loadgen consumes it via the
DISC_STAGES env (symbolic targets BASELINE/SPIKE resolved there). Varying
the arrangement PER ATTEMPT keeps an agent from hardcoding the timeline it saw
in its previous trace — only a signal-driven policy passes every arrangement.
"""
from __future__ import annotations

import json
import os
import sys

CURATED = ["BBS", "BSB", "SBB", "SSB", "SBS", "BSS"]


def main() -> None:
    ws = sys.argv[1]
    shape = os.environ.get("VERIFY_SHAPE", "").upper().strip()
    seed = os.environ.get("VERIFY_SHAPE_SEED", "").strip()
    if shape:
        if not shape or set(shape) - {"B", "S"} or "S" not in shape:
            raise SystemExit(f"VERIFY_SHAPE must be a string of B/S with >=1 S, got {shape!r}")
    elif seed:
        # round-robin over the curated arrangements: seed = attempt number
        # cycles them deterministically and uniformly
        shape = CURATED[int(seed) % len(CURATED)]
    else:
        raise SystemExit("need VERIFY_SHAPE or VERIFY_SHAPE_SEED")

    multi = shape.count("S") >= 2
    base = int(os.environ["DISC_T_BASELINE"]) if "DISC_T_BASELINE" in os.environ else (15 if multi else 8)
    ramp = int(os.environ.get("DISC_T_RAMP") or 20)
    spk = int(os.environ["DISC_T_SPIKE"]) if "DISC_T_SPIKE" in os.environ else (25 if multi else 30)
    tail = int(os.environ["DISC_T_TAIL"]) if "DISC_T_TAIL" in os.environ else 12

    stages: list[dict] = []
    windows: list[dict] = []
    t = 0.0

    def add(target: str, dur: float) -> None:
        nonlocal t
        stages.append({"target": target, "duration": f"{dur}s"})
        t += dur

    for ev in shape:
        add("BASELINE", base)
        w0 = t
        if ev == "B":
            add("BASELINE", ramp + 2)
            windows.append({"kind": "blip", "t0": w0, "t1": t})
        else:
            add("SPIKE", ramp); add("SPIKE", spk); add("BASELINE", 3)
            windows.append({"kind": "spike", "t0": w0, "t1": t})
    add("BASELINE", tail)

    with open(os.path.join(ws, "schedule.json"), "w") as fh:
        json.dump({"shape": shape, "seed": seed or None, "total_s": t,
                   "events": windows, "stages": stages}, fh, indent=1)
    print(json.dumps(stages))


if __name__ == "__main__":
    main()
