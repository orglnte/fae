"""What exists once per process, for the whole process: the experiment it
runs, that experiment's definition and its workspace. Each function here is
a dependency its callers reach for instead of receiving it."""

_current: dict = {"x": None}


def current():
    """The experiment this process runs, built on first use from the
    environment: REPO_ROOT (else the working directory) and WORKSPACES_DIR."""
    if _current["x"] is None:
        from fae.experiment import Experiment
        _current["x"] = Experiment()
    return _current["x"]


def workspace():
    """The workspace this process runs on: current().workspace."""
    return current().workspace


def definition():
    """The definition this process runs: current().definition."""
    return current().definition


def set_current(experiment):
    """Make `experiment` the one this process runs (tests only); returns the
    previous one."""
    prev, _current["x"] = _current["x"], experiment
    return prev
