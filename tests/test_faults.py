"""fae/cell/faults.py against every wall wording the corpus has shown.

The fixtures are the lines real CLIs printed; a wording that is not here has
not been proven to classify.
"""
import os
import time
import unittest
from datetime import datetime, timezone

from _ctx import ROOT  # noqa: F401  (puts the repo root on sys.path)
from fae.cell import faults
from fae.testagent import AUTH_LINE, LIMIT429_LINES, LIMIT_LINE

CLAUDE_429 = "\n".join(LIMIT429_LINES) + "\n"


def claude_result(text, status=None, is_error=True):
    """A stream-json transcript whose last line is a result carrying `text`."""
    st = f'"api_error_status":{status},' if status is not None else ""
    return ('{"type":"system","subtype":"init","model":"claude-sonnet-5"}\n'
            '{"type":"result","subtype":"success",'
            f'"is_error":{"true" if is_error else "false"},{st}'
            f'"result":"{text}","num_turns":1}}\n')


class TestTheStructuredResultLine(unittest.TestCase):
    def test_the_weekly_wall_is_a_limit_whatever_the_exit_code(self):
        for rc in (0, 1):
            with self.subTest(rc=rc):
                self.assertEqual(faults.classify(CLAUDE_429, rc), faults.LIMIT)

    def test_the_result_text_is_the_reason(self):
        self.assertEqual(faults.reason(CLAUDE_429),
                         "You've hit your weekly limit · resets 12am (UTC)")

    def test_every_observed_claude_wording(self):
        cases = {
            "You've hit your session limit · resets 7:40pm (UTC)": faults.LIMIT,
            "You've hit your weekly limit · resets Jul 29, 12am (UTC)": faults.LIMIT,
            "API Error: Connection closed mid-response. The response above may be incomplete.": faults.LIMIT,
            "API Error: Unable to connect to API (ConnectionRefused)": faults.LIMIT,
            "API Error: Unable to connect to API (ECONNRESET)": faults.LIMIT,
            "API Error: Unable to connect to API (FailedToOpenSocket)": faults.LIMIT,
            "API Error: 500 Internal server error": faults.LIMIT,
            "API Error: 529 Overloaded": faults.LIMIT,
            "There's an issue with the selected model (human). It may not exist or you may not have access to it.": faults.AUTH,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(faults.classify(claude_result(text), 0), want)

    def test_a_status_alone_decides(self):
        for status, want in ((429, faults.LIMIT), (500, faults.LIMIT),
                             (529, faults.LIMIT), (401, faults.AUTH),
                             (403, faults.AUTH), (404, faults.AUTH)):
            with self.subTest(status=status):
                self.assertEqual(
                    faults.classify(claude_result("x", status=status), 0), want)

    def test_a_finished_run_that_talks_about_limits_is_judged(self):
        t = claude_result("Added exponential backoff for the 429 rate limit case",
                          is_error=False)
        self.assertIsNone(faults.classify(t, 0))
        self.assertIsNone(faults.classify(t, 0, gate_rc=False))

    def test_an_error_result_with_no_wall_wording_is_judged(self):
        self.assertIsNone(faults.classify(claude_result("some tool failed"), 1))


class TestPlainTextTranscripts(unittest.TestCase):
    GEMINI_QUOTA = ("Error: Individual quota reached. Please upgrade your "
                    "subscription to increase your limits. Resets in 126h54m56s.\n")
    OPENCODE = ('level=ERROR message="stream error" providerID=opencode-go '
                'error.error="AI_APICallError: Monthly usage limit reached. '
                'Resets in 21 days. To continue using this model now, enable '
                'usage from your available balance"\n')
    OPENCODE_RETRY = ('error.error="AI_RetryError: Failed after 3 attempts. '
                      'Last error: Monthly usage limit reached. Resets in 21 days"\n')
    # gemini_high_beta_apidocs_T1_r14 attempt 1 (2026-08-25T19:01Z):
    # the whole 154-byte transcript, charged as stage=no-edit.
    GEMINI_ELIGIBILITY = (
        'Error: Eligibility check failed: failed to get load code assist '
        'response: Post "https://daily-cloudcode-pa.googleapis.com/'
        'v1internal:loadCodeAssist": EOF\n')

    def test_the_rc_gate_holds_for_prose(self):
        self.assertEqual(faults.classify(LIMIT_LINE + "\n", 1), faults.LIMIT)
        self.assertIsNone(faults.classify(LIMIT_LINE + "\n", 0))

    def test_an_untouched_tree_lifts_the_gate(self):
        self.assertEqual(faults.classify(LIMIT_LINE + "\n", 0, gate_rc=False),
                         faults.LIMIT)

    def test_every_observed_plain_wording(self):
        for text, want in ((self.GEMINI_QUOTA, faults.LIMIT),
                           (self.OPENCODE, faults.LIMIT),
                           (self.OPENCODE_RETRY, faults.LIMIT),
                           (self.GEMINI_ELIGIBILITY, faults.LIMIT),
                           (AUTH_LINE + "\n", faults.AUTH)):
            with self.subTest(text=text[:40]):
                self.assertEqual(faults.classify(text, 1), want)

    def test_a_gemini_eligibility_eof_is_a_limit_not_a_build(self):
        """The CLI died before its first model call (EOF on loadCodeAssist);
        the tree is untouched. A retry, not a charged no-edit attempt, and
        not a wall the lane must cool for."""
        t = self.GEMINI_ELIGIBILITY
        self.assertEqual(faults.classify(t, 1), faults.LIMIT)
        self.assertIsNone(faults.classify(t, 0))
        self.assertEqual(faults.classify(t, 0, gate_rc=False), faults.LIMIT)
        self.assertFalse(faults.quota_wall(t))
        self.assertIsNone(faults.reset_hint_s(t))
        self.assertEqual(faults.reason(t), t.strip())

    def test_kubectl_unauthorized_is_not_an_auth_wall(self):
        t = "error: You must be logged in to the server (Unauthorized)\n"
        self.assertIsNone(faults.classify(t, 1))

    def test_hex_and_ports_are_not_status_codes(self):
        t = "sha256:429abc5001e0 listening on :5000 (500 rps)\n"
        self.assertIsNone(faults.classify(t, 1))


class TestQuotaWalls(unittest.TestCase):
    def test_walls_the_lane_must_cool_for(self):
        for text in ("You've hit your weekly limit · resets 12am (UTC)",
                     "You've hit your session limit · resets 7:40pm (UTC)",
                     "Individual quota reached. Resets in 11m35s.",
                     "Monthly usage limit reached. Resets in 21 days.",
                     "API Error: Claude usage limit reached. Resets in 3 hours",
                     "429 Too Many Requests"):
            with self.subTest(text=text):
                self.assertTrue(faults.quota_wall(text))

    def test_transport_faults_are_not_walls(self):
        for text in ("API Error: Connection closed mid-response.",
                     "API Error: Unable to connect to API (ConnectionRefused)",
                     "API Error: 529 Overloaded"):
            with self.subTest(text=text):
                self.assertFalse(faults.quota_wall(text))


class TestResetHints(unittest.TestCase):
    NOW = datetime(2026, 8, 25, 20, 2, 17, tzinfo=timezone.utc)

    def hint(self, text):
        return faults.reset_hint_s(text, now=self.NOW)

    def test_durations(self):
        self.assertEqual(self.hint("Resets in 3 hours"), 3 * 3600)
        self.assertEqual(self.hint("Resets in 21 days."), 21 * 86400)
        self.assertEqual(self.hint("Resets in 11m35s."), 11 * 60 + 35)
        self.assertEqual(self.hint("Resets in 1h12m15s."), 3600 + 12 * 60 + 15)
        self.assertEqual(self.hint("Resets in 126h54m56s."),
                         126 * 3600 + 54 * 60 + 56)
        self.assertEqual(self.hint("retry after 90 seconds"), 90)

    def test_clock_times_are_the_next_such_instant_utc(self):
        self.assertEqual(self.hint("resets 12am (UTC)"), 3 * 3600 + 57 * 60 + 43)
        self.assertEqual(self.hint("resets 7:40pm (UTC)"), 23 * 3600 + 37 * 60 + 43)
        self.assertEqual(self.hint("resets 11:30pm (UTC)"), 3 * 3600 + 27 * 60 + 43)

    def test_a_dated_clock_time(self):
        self.assertEqual(self.hint("resets Aug 29, 12am (UTC)"),
                         3 * 86400 + 3 * 3600 + 57 * 60 + 43)
        # a date already past this year means next year
        self.assertEqual(self.hint("resets Jul 29, 12am (UTC)"),
                         (datetime(2027, 7, 29, tzinfo=timezone.utc)
                          - self.NOW).total_seconds())

    def test_no_hint(self):
        self.assertIsNone(self.hint("API Error: 529 Overloaded"))
        self.assertIsNone(self.hint("Resets in 0 seconds"))
        self.assertIsNone(self.hint("resets Foo 31, 12am (UTC)"))


if __name__ == "__main__":
    unittest.main()


class TestAPlainTranscriptIsOnlyTheCLIsOwnWords(unittest.TestCase):
    """The task IS rate limiting: an agent writes `HTTPException(429,
    "rate limit")` and kubectl prints "connection refused". Across a whole
    transcript only what the CLI itself prints when walled may count. The
    first fleet run of validator rule 11 tainted 222 real cells on task
    prose before this split."""

    PROSE = ('raise HTTPException(status_code=429, detail="rate limit")\n'
             'E0825 dial tcp 10.0.0.1:443: connection refused\n'
             'retrying after 529 overloaded response from the store\n'
             'level=INFO message="stream error handled by the client"\n')

    def test_task_prose_is_not_a_wall_even_ungated(self):
        self.assertIsNone(faults.classify(self.PROSE, 1))
        self.assertIsNone(faults.classify(self.PROSE, 1, gate_rc=False))

    def test_the_cli_wordings_still_are(self):
        for text in (LIMIT_LINE + "\n",
                     "Error: Individual quota reached. Resets in 3h.\n",
                     'error.error="AI_APICallError: Monthly usage limit reached."\n',
                     'error.error="AI_RetryError: Failed after 3 attempts."\n'):
            with self.subTest(text=text[:30]):
                self.assertEqual(faults.classify(self.PROSE + text, 1), faults.LIMIT)

    def test_the_structured_text_keeps_the_wider_vocabulary(self):
        self.assertEqual(faults.classify(
            claude_result("API Error: Connection closed mid-response."), 0),
            faults.LIMIT)

    def test_reason_names_the_cli_line_not_the_prose(self):
        text = self.PROSE + "Error: Individual quota reached. Resets in 3h.\n"
        self.assertTrue(faults.reason(text).startswith("Error: Individual quota"))


# The Claude CLI keeps writing after its result line: every background task
# it kills on exit gets a system line. Verbatim shape from
# opus_high_alpha_apidocs_T1_r9 attempt 1 (ids kept).
POST_RESULT_TAIL = (
    '{"type":"system","subtype":"task_updated","task_id":"bial7dzqs",'
    '"patch":{"status":"killed","end_time":1786778121895},'
    '"uuid":"20d7f759-467b-4423-8b33-b844d55d5fb0"}\n'
    '{"type":"system","subtype":"task_notification","task_id":"bial7dzqs",'
    '"tool_use_id":"toolu_01Ur1MMptEhr7r9BWNGq4yAT","status":"stopped",'
    '"output_file":"","summary":"Wait for load run to finish",'
    '"uuid":"121fb34d-7611-4f04-bc4f-913753f0c77d"}\n')

ASSISTANT_SAYS_RESULT = (
    '{"type":"assistant","message":{"role":"assistant",'
    '"content":[{"type":"text","text":"result"}]}}\n')


class TestTheResultLineIsFoundFromTheTail(unittest.TestCase):
    def test_the_task_lines_the_cli_writes_after_its_verdict_do_not_hide_it(self):
        t = CLAUDE_429 + POST_RESULT_TAIL
        self.assertEqual(faults.classify(t, 0), faults.LIMIT)
        self.assertEqual(faults.reason(t),
                         "You've hit your weekly limit · resets 12am (UTC)")

    def test_a_tail_line_cut_mid_write_does_not_hide_the_verdict(self):
        t = (CLAUDE_429 + '{"type":"system","subtype":"task_notification",'
             '"task_id":"bial7dzqs","summary":"result","uuid":"121fb')
        self.assertEqual(faults.classify(t, 0), faults.LIMIT)

    def test_only_a_result_typed_line_is_the_verdict(self):
        self.assertEqual(faults.classify(CLAUDE_429 + ASSISTANT_SAYS_RESULT, 0),
                         faults.LIMIT)
        killed = ('{"type":"system","subtype":"init","model":"claude-sonnet-5"}\n'
                  + ASSISTANT_SAYS_RESULT)
        self.assertIsNone(faults.parse_result_line(killed))

    def test_a_verdict_without_text_reads_as_empty_text(self):
        for line in ('{"type":"result","subtype":"error_during_execution",'
                     '"is_error":true,"api_error_status":null,"num_turns":3}',
                     '{"type":"result","subtype":"success","is_error":true,'
                     '"api_error_status":null,"result":null}'):
            with self.subTest(line=line[:60]):
                r = faults.parse_result_line(line + "\n")
                self.assertEqual(r, faults.AgentResult(None, True, ""))
                # an empty verdict names nothing: the reason is the last wall
                # line the transcript itself printed
                self.assertEqual(
                    faults.reason("API Error: 529 Overloaded\n" + line + "\n"),
                    "API Error: 529 Overloaded")


class TestTheReasonLine(unittest.TestCase):
    def test_the_reason_is_the_last_wall_line_even_when_the_transcript_goes_on(self):
        for wall in (AUTH_LINE, LIMIT_LINE):
            with self.subTest(wall=wall[:30]):
                t = ("Reading seeds/README.md\n" + wall + "\n"
                     "Press Enter to continue…\n\n")
                self.assertEqual(faults.reason(t), wall)

    def test_without_a_wall_the_reason_is_the_last_nonblank_line(self):
        t = "Reading seeds/README.md\nWriting app.py\n\nexit status 137\n\n  \n"
        self.assertEqual(faults.reason(t), "exit status 137")

    def test_an_empty_transcript_has_no_reason(self):
        self.assertEqual(faults.reason(""), "")
        self.assertEqual(faults.reason("\n  \n"), "")


class TestResetHintBoundaries(unittest.TestCase):
    NOW = TestResetHints.NOW

    def test_the_shortest_hint_is_one_second(self):
        self.assertEqual(faults.reset_hint_s("retry after 1s"), 1)
        self.assertEqual(faults.reset_hint_s("Resets in 1 second."), 1)
        self.assertIsNone(faults.reset_hint_s("Resets in 0s"))

    def test_the_corpus_minute_wordings(self):
        self.assertEqual(faults.reset_hint_s("Weekly usage limit reached. Resets in 17min."),
                         17 * 60)
        self.assertEqual(faults.reset_hint_s("Resets in 2h15m55s."),
                         2 * 3600 + 15 * 60 + 55)
        self.assertEqual(faults.reset_hint_s("Resets in 6m45s."), 6 * 60 + 45)

    def test_a_clock_time_equal_to_now_means_the_next_one(self):
        now = datetime(2026, 8, 26, 0, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(faults.reset_hint_s("resets 12am (UTC)", now=now), 86400)
        self.assertEqual(faults.reset_hint_s("resets Aug 26, 12am (UTC)", now=now),
                         365 * 86400)

    def test_fractions_of_a_second_are_dropped(self):
        whole = faults.reset_hint_s("resets 12am (UTC)", now=self.NOW)
        self.assertEqual(whole, 3 * 3600 + 57 * 60 + 43)
        late = self.NOW.replace(microsecond=999_999)
        self.assertEqual(faults.reset_hint_s("resets 12am (UTC)", now=late),
                         whole - 1)

    @unittest.skipUnless(hasattr(time, "tzset"), "needs a settable host zone")
    def test_the_default_clock_is_utc_not_the_host_zone(self):
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        time.tzset()
        try:
            want = faults.reset_hint_s("resets 12am (UTC)",
                                       now=datetime.now(timezone.utc))
            got = faults.reset_hint_s("resets 12am (UTC)")
        finally:
            if saved is None:
                del os.environ["TZ"]
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.assertLess(abs(got - want), 5)
