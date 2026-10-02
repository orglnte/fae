"""elastic_resource — the verifier block for a 0<->1 resource under a load
shape. The law any provision-under-load policy is judged by: reject the
blips, mount within a deadline of the store's SATURATION under the
sustained spike, hold through it, release when traffic drains, never flap,
serve every request. The spike is a ramp, so it crosses the store's knee
wherever the host puts it, and the deadline runs from that crossing (the
signal a policy can see), never from the load generator's clock; a spike
that never saturates the store is a void, not a verdict.

Four instruments, run through fae/cell/verify.py's loader:

    load_shape   the arrangement (a string of B/S events) -> k6 stages and
                 the schedule.json the law reads its windows from
    trace        the per-second sensor beside the load generator: offered
                 load, latency, the resource's own claim (a /health field),
                 an infra probe that confirms it, the mount/release epochs
    k6           the load numbers out of k6's summary, and the mount delay
    law          the verdict: episodes against the schedule's windows

The experiment supplies the parameters: the arrangements and their timing,
the health fields that name the resource's state, the probe that proves it
is really serving, and the error and mount-delay thresholds.
"""
from pathlib import Path

DIR = Path(__file__).resolve().parent
