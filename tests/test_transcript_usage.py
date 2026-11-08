"""The transcript's account of a run (fae/cell/transcript.py) and the scorer's
totals of it (score_cell.parse_usage): API time, wall minus API time, turns,
tool calls, tokens, retries and their backoff."""
import json
import tempfile
import unittest
from pathlib import Path

from _ctx import ROOT  # noqa: F401

from fae.cell import clients, transcript
from fae.experiment.scoring import score_cell as sc


def _result(**kw):
    e = {"type": "result", "duration_ms": 900000, "duration_api_ms": 400000,
         "num_turns": 50, "total_cost_usd": 0.4,
         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                   "cache_read_input_tokens": 1000, "output_tokens": 9000,
                   "output_tokens_details": {"thinking_tokens": 6000}}}
    e.update(kw)
    return e


def _assistant(*ids):
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": i} for i in ids]
                        + [{"type": "text", "text": "x"}]}}


def _log(events, d=None):
    d = Path(d or tempfile.mkdtemp())
    p = d / "agent.attempt-1.log"
    p.write_text("".join((json.dumps(e) if isinstance(e, dict) else e) + "\n" for e in events))
    return p


class TestOneRun(unittest.TestCase):
    def test_the_result_event_and_the_tool_blocks_are_read(self):
        u = clients.reader("claude").usage(_log([_assistant("a", "b"), "plain text line",
                                             _assistant("c"), _result()]))
        self.assertEqual(u, {"api_s": 400.0, "turns": 50, "tool_calls": 3, "out_tok": 9000,
                             "think_tok": 6000, "in_tok": 1110, "cost_usd": 0.4,
                             "retries": 0, "retry_wait_s": 0.0, "limited": 0})

    def test_a_tool_block_streamed_twice_counts_once(self):
        u = clients.reader("claude").usage(_log([_assistant("a"), _assistant("a", "b"), _result()]))
        self.assertEqual(u["tool_calls"], 2)

    def test_retries_their_backoff_and_rate_limit_pressure_are_counted(self):
        u = clients.reader("claude").usage(_log([
            {"type": "system", "subtype": "api_retry", "retry_delay_ms": 542},
            {"type": "system", "subtype": "api_retry", "retry_delay_ms": 39776},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}},
            _result()]))
        self.assertEqual((u["retries"], u["retry_wait_s"], u["limited"]), (2, 40.3, 1))


class TestAResumedRun(unittest.TestCase):
    """A finished background task resumes the session, which closes with a
    result event again: API time and cost run on, turns and tokens restart."""

    def test_running_totals_are_read_last_and_counts_are_summed(self):
        u = clients.reader("claude").usage(_log([
            _result(duration_api_ms=410000, num_turns=67, total_cost_usd=0.40),
            _result(duration_api_ms=446000, num_turns=3, total_cost_usd=0.56,
                    usage={"input_tokens": 1, "cache_creation_input_tokens": 2,
                           "cache_read_input_tokens": 3, "output_tokens": 100,
                           "output_tokens_details": {"thinking_tokens": 40}})]))
        self.assertEqual((u["api_s"], u["cost_usd"]), (446.0, 0.56))
        self.assertEqual((u["turns"], u["out_tok"], u["think_tok"], u["in_tok"]),
                         (70, 9100, 6040, 1116))


def _agy(*steps, usage=None, duration=851.3):
    """An agy stream-json transcript with the fields its real ones carry."""
    out = [{"event": "init", "conversation_id": "c",
            "init": {"model": "Gemini 3.8 Flash (High)", "permission_mode": "always-proceed"}}]
    for i, kind in steps:
        for state in ("ACTIVE", "DONE"):
            out.append({"event": "step_update", "step_update": {
                "conversation_id": "c", "step_index": i, "state": state, "step_type": kind}})
    out.append({"event": "result", "result": {
        "conversation_id": "c", "status": "SUCCESS", "response": "done",
        "duration_seconds": duration, "num_turns": 1,
        "usage": usage or {"input_tokens": 386010, "output_tokens": 82250,
                           "thinking_tokens": 66030, "cache_read_tokens": 6996400,
                           "total_tokens": 468260}}})
    return out


class TestAgy(unittest.TestCase):
    def test_turns_and_tools_are_its_steps_counted_once(self):
        u = clients.reader("agy").usage(_log(_agy((0, "user_input"), (1, "agent_response"),
                                               (2, "tool"), (3, "agent_response"),
                                               (4, "tool"), (5, "agent_response"))))
        self.assertEqual((u["turns"], u["tool_calls"]), (3, 2))
        self.assertEqual((u["out_tok"], u["think_tok"], u["in_tok"]),
                         (82250, 66030, 386010 + 6996400))

    def test_a_command_its_sandbox_refused_to_start_is_counted_once(self):
        """agy's sandbox sometimes cannot start a command at all; the agent
        loses the call through no fault of its own."""
        from fae.cell.clients import agy
        log = _agy((1, "agent_response"), (2, "tool"), (3, "tool"))
        for e in log:
            st = e.get("step_update") or {}
            if st.get("step_index") == 2 and st.get("state") == "DONE":
                st["tool_info"] = {"name": "run_command",
                                   "output": f"Command failed to run: {agy.SANDBOX_REFUSED}.\n"}
        u = clients.reader("agy").usage(_log(log))
        self.assertEqual((u["tool_calls"], u["sandbox_fails"]), (2, 1))

    def test_a_run_its_sandbox_never_refused_counts_zero(self):
        u = clients.reader("agy").usage(_log(_agy((1, "agent_response"), (2, "tool"))))
        self.assertEqual(u["sandbox_fails"], 0)

    def test_what_agy_does_not_record_is_unknown_not_zero(self):
        u = clients.reader("agy").usage(_log(_agy((1, "agent_response"))))
        for f in ("api_s", "cost_usd", "retries", "retry_wait_s", "limited"):
            self.assertIsNone(u[f], f)

    def test_its_own_duration_is_the_clients_account(self):
        self.assertEqual(clients.reader("agy").client_seconds(_log(_agy())), (851.3, 1))

    def test_no_result_event_reads_as_none(self):
        self.assertIsNone(clients.reader("agy").usage(_log(_agy()[:-1])))


class TestNoAccount(unittest.TestCase):
    def test_other_clients_and_missing_results_read_as_none(self):
        log = _log([_result()])
        self.assertIsNone(clients.reader("agy").usage(log))         # a claude transcript
        self.assertIsNone(clients.reader("opencode").usage(log))
        self.assertIsNone(clients.reader("claude").usage(_log([_assistant("a")])))
        self.assertIsNone(clients.reader("claude").usage(Path("/nonexistent/log")))

    def test_a_missing_token_count_leaves_that_total_unknown(self):
        u = clients.reader("claude").usage(_log([_result(usage={"output_tokens": 5})]))
        self.assertIsNone(u["in_tok"])
        self.assertIsNone(u["think_tok"])
        self.assertEqual(u["out_tok"], 5)


class _Cell:
    def __init__(self, d):
        self.d = Path(d)

    def agent_log(self, n):
        return self.d / f"agent.attempt-{n}.log"


class TestTheScorersTotals(unittest.TestCase):
    def _cell(self, attempts):
        d = tempfile.mkdtemp()
        for n, events in attempts.items():
            (Path(d) / f"agent.attempt-{n}.log").write_text(
                "".join(json.dumps(e) + "\n" for e in events))
        return _Cell(d)

    def _time(self, *rows):
        text = "".join("2026-10-01T00:00:00Z\t" + "\t".join(r) + "\n" for r in rows)
        return sc.parse_agent_time(text)

    def test_tool_s_is_the_judged_runs_wall_minus_api_time(self):
        t = self._time(("AGENT", "c", "attempt=1", "s=1000", "runs=2", "last_s=700",
                        "client=claude:2.1.296"), ("ITER", "green", "attempt=1"))
        u = sc.parse_usage(self._cell({1: [_result()]}), t)
        self.assertEqual((u["api_s_total"], u["tool_s_total"]), (400.0, 300.0))

    def test_attempts_up_to_green_are_summed_and_later_ones_kept_per_attempt(self):
        t = self._time(("AGENT", "c", "attempt=1", "s=900", "last_s=900", "client=claude:2"),
                       ("ITER", "fail", "attempt=1"),
                       ("AGENT", "c", "attempt=2", "s=500", "last_s=500", "client=claude:2"),
                       ("ITER", "green", "attempt=2"))
        u = sc.parse_usage(self._cell({1: [_result()], 2: [_result(num_turns=10)]}), t)
        self.assertEqual((u["turns_total"], u["tool_s_total"]), (60, 600.0))
        self.assertEqual(sorted(u["usage_by_attempt"]), ["1", "2"])

    def test_a_total_is_unknown_when_one_attempt_has_no_account(self):
        t = self._time(("AGENT", "c", "attempt=1", "s=900", "last_s=900", "client=agy:1"),
                       ("ITER", "fail", "attempt=1"),
                       ("AGENT", "c", "attempt=2", "s=500", "last_s=500", "client=claude:2"),
                       ("ITER", "green", "attempt=2"))
        u = sc.parse_usage(self._cell({1: [_result()], 2: [_result()]}), t)
        self.assertIsNone(u["turns_total"])
        self.assertEqual(list(u["usage_by_attempt"]), ["2"])

    def test_every_field_has_a_total(self):
        t = self._time(("AGENT", "c", "attempt=1", "s=9", "last_s=9", "client=claude:2"),
                       ("ITER", "green", "attempt=1"))
        u = sc.parse_usage(self._cell({1: [_result()]}), t)
        self.assertEqual({k for k in u if k != "usage_by_attempt"},
                         {f"{f}_total" for f in sc.USAGE_FIELDS} | {"api_ms_per_out_tok"})
        self.assertEqual(u["api_ms_per_out_tok"], round(400000 / 9000, 2))

    def test_no_output_tokens_leaves_the_speed_unknown(self):
        t = self._time(("AGENT", "c", "attempt=1", "s=9", "last_s=9", "client=claude:2"),
                       ("ITER", "green", "attempt=1"))
        u = sc.parse_usage(self._cell({1: [_result(usage={"output_tokens": 0})]}), t)
        self.assertIsNone(u["api_ms_per_out_tok"])


if __name__ == "__main__":
    unittest.main()
