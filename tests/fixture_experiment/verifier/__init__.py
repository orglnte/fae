"""The fixture's verifier: green iff answer.txt says 42. FIXTURE_VERDICT in
the environment (a Verdict as JSON) overrides the judgment, so a test can
drive any stage, refund or stand-down through the real boundary."""
import os
from pathlib import Path

from fae.cell.verify import Verdict, Verifier


class FixtureVerifier(Verifier):
    IMAGE_DIR = Path(__file__).parent
    FILES = ("verify.log",)
    FEEDBACK_LOGS = ("verify.log", "deploy.log", "service.deploy.log", "cluster-diag")

    def verify(self, ctx):
        forced = os.environ.get("FIXTURE_VERDICT")
        if forced:
            v = Verdict.from_json(forced)
            return Verdict(**{**v.__dict__, "arrangement": ctx.arrangement})
        answer = (Path(ctx.artifacts) / "answer.txt")
        got = answer.read_text().strip() if answer.is_file() else ""
        (Path(ctx.out) / "verify.log").write_text(f"answer={got!r}\n")
        ok = got == "42"
        return Verdict(ok=ok, stage="" if ok else "answer", why="" if ok else f"got {got!r}",
                       metrics={"answer": got}, arrangement=ctx.arrangement)
