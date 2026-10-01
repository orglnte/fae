"""The engine suite's own experiment: two variants, one with an exclusive
lock, three arrangements, a verifier that judges a text file. Nothing here
provisions anything; the engine's locks, queues, loop and verdict paths run
against it exactly as against a real experiment."""
from fae.cell.experiment import Gate

NAME = "fixture"


def variant_classes():
    from .variants import VARIANTS
    return VARIANTS


MATRIX = {"alpha": ["apidocs", "howto", "openbook", "onlysrc"], "beta": ["apidocs", "onlysrc"]}

SEED_DOCS = {
    ("alpha", "apidocs"): ("any.alpha.apidocs.api.md", 1),
    ("alpha", "howto"): ("any.alpha.howto.api.md", 1),
    ("alpha", "openbook"): ("any.alpha.api.md", 1),
    ("alpha", "onlysrc"): ("any.alpha.api.md", 1),
    ("beta", "apidocs"): ("any.beta.apidocs.api.md", 1),
    ("beta", "onlysrc"): ("any.beta.api.md", 1),
}

# Model ids folded into one scoreboard row (the engine's pooling, exercised
# here on the ids the scoreboard tests use).
POOLED_MODELS = {"claude-fable-5": "fable-5.1/5", "claude-fable-5-1": "fable-5.1/5",
                 "Gemini 3.6 Flash (High)": "gemini-3.8f/3.6f", "Gemini 3.8 Flash (High)": "gemini-3.8f/3.6f", "Gemini 3.1 Pro (High)": "g31pro"}

GATE = Gate(("G1", "G2", "G3", "G4", "G5", "G6"), feedback_note=(
    "NOTE: verification runs several arrangements. Your build passed: {passed}\n"
    "but FAILED on arrangement: {failed}.\n"
    "ALL arrangements must pass.\n"))


def verifier_class():
    from .verifier import FixtureVerifier
    return FixtureVerifier
