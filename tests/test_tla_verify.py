"""fae/utils/tla_verify.py: a live trace replays from its last EPOCH block."""
import importlib.machinery
import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "fae" / "utils" / "tla_verify.py"


def _tool():
    loader = importlib.machinery.SourceFileLoader("tla_verify", str(TOOL))
    spec = importlib.util.spec_from_loader("tla_verify", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _log(tmp_path, *lines):
    p = tmp_path / "transitions.log"
    p.write_text("".join(l + "\n" for l in lines))
    return p


def _kept(tmp_path, *lines):
    tv = _tool()
    return [(e["action"], e["cid"]) for e in tv._from_last_epoch(tv._parse_transitions(_log(tmp_path, *lines)))]


def test_a_log_without_epoch_is_replayed_whole(tmp_path):
    assert _kept(tmp_path,
                 "2026-10-03T10:00:00Z\tAdmit\ta\t",
                 "2026-10-03T10:00:05Z\tCrash\ta\t") == [("Admit", "a"), ("Crash", "a")]


def test_the_last_epoch_run_drops_everything_written_before_it(tmp_path):
    assert _kept(tmp_path,
                 "2026-10-03T10:00:00Z\tEPOCH\ta\tloop=none",
                 "2026-10-03T10:00:01Z\tAdmit\ta\t",
                 "2026-10-03T10:00:02Z\tPause\tb\t",
                 "# transitions log RESET",
                 "2026-10-03T11:00:00Z\tEPOCH\ta\tloop=none",
                 "2026-10-03T11:00:01Z\tEPOCH\tb\tintent=paused",
                 "2026-10-03T11:00:02Z\tResume\tb\t") == [
        ("EPOCH", "a"), ("EPOCH", "b"), ("Resume", "b")]


def test_an_event_timestamped_before_the_epoch_but_appended_after_it_is_kept(tmp_path):
    assert _kept(tmp_path,
                 "2026-10-03T10:00:01Z\tAdmit\ta\t",
                 "2026-10-03T11:00:00Z\tEPOCH\ta\tloop=none",
                 "2026-10-03T10:59:59Z\tCrash\tb\t") == [("Crash", "b"), ("EPOCH", "a")]
