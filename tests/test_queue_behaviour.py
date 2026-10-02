"""The backlog on disk: a spec is one FILE, its state is the directory it sits
in, lane order is the filename sort, and sequence numbers never collide.

Every test runs against its own temp .orch tree (OrchTmpCase); nothing here
touches the live fleet. Paths are compared as Path objects or by their
relative parts, never by existence alone — the host filesystem is
case-insensitive, so `orch/queue` and `orch/QUEUE` are the same directory
and only the spelling in the returned path tells the two apart.
"""
import contextlib
import io
import re
import unittest
from datetime import datetime, timezone

from _ctx import OrchTmpCase, runs

queue = runs.queue
SEQ = queue.SEQ_START
AGENT = "aaa"


def spec(variant="beta_apidocs", rep=1, task="T1"):
    return dict(task=task, variant=variant, rep=rep)


def cid_of(agent=AGENT, rep=1, task="T1", variant="beta_apidocs"):
    return runs.cell_id(agent, variant, rep, task)


def seq_of(p):
    return int(p.name.split(".", 1)[0])


class QueueCase(OrchTmpCase):

    def rel(self, p):
        return p.relative_to(self.orch).parts

    def everywhere(self, cid):
        """Every file under the .orch tree that names this cid, as parts
        relative to .orch — the set must always have exactly one member."""
        return sorted(self.rel(p) for p in self.orch.rglob(f"*{cid}.json"))


class TestASpecIsInExactlyOneState(QueueCase):

    def test_each_transition_moves_the_one_file_between_the_named_dirs(self):
        cid = cid_of()
        p = queue.enqueue(AGENT, spec())
        self.assertEqual(self.rel(p), ("queue", AGENT, f"{SEQ:06d}.{cid}.json"))
        self.assertEqual(self.everywhere(cid), [self.rel(p)])

        r = queue.claim(AGENT, p)
        self.assertEqual(self.rel(r), ("running", AGENT, f"{cid}.json"))
        self.assertEqual(self.everywhere(cid), [self.rel(r)])
        self.assertEqual(queue.running_specs(AGENT), [r])
        self.assertEqual(queue.running_specs(), [r])
        self.assertEqual(queue.lane_specs(AGENT), [])

        back = queue.release(AGENT, r)
        self.assertEqual(self.rel(back), ("queue", AGENT, f"{SEQ:06d}.{cid}.json"))
        self.assertEqual(self.everywhere(cid), [self.rel(back)])
        self.assertEqual(queue.running_specs(), [])

        d = queue.finish(AGENT, queue.claim(AGENT, back))
        self.assertEqual(self.rel(d), ("done", AGENT, f"{cid}.json"))
        self.assertEqual(self.everywhere(cid), [self.rel(d)])
        self.assertEqual(queue.read_spec(d), spec())

    def test_the_lane_dirs_are_the_spelled_out_queue_running_done_paths(self):
        queue.enqueue(AGENT, spec())
        self.assertEqual(queue.lane_dir(AGENT), self.orch / "queue" / AGENT)
        self.assertEqual(queue.lane_dir(AGENT, parked=True),
                         self.orch / "queue" / f"{AGENT}.parked")
        self.assertEqual(queue.rundir(AGENT), self.orch / "running" / AGENT)
        self.assertEqual(queue.lane_dirs(), [self.orch / "queue" / AGENT])

    def test_a_second_claim_on_a_cid_is_refused_and_named(self):
        cid = cid_of()
        queue.claim(AGENT, queue.enqueue(AGENT, spec()))
        dup = queue.lane_dir(AGENT) / f"{SEQ + 1:06d}.{cid}.json"
        dup.write_text("{}\n")
        with self.assertRaises(FileExistsError) as err:
            queue.claim(AGENT, dup)
        self.assertIn(cid, str(err.exception))
        self.assertTrue(dup.exists())
        self.assertEqual(len(queue.running_specs(AGENT)), 1)

    def test_release_recreates_a_lane_whose_queue_tree_is_gone(self):
        # A claim adopted into a fresh .orch has no queue/ at all; releasing
        # it must build the whole path, not just the leaf.
        cid = cid_of()
        r = queue.rundir(AGENT) / f"{cid}.json"
        r.parent.mkdir(parents=True)
        r.write_text("{}\n")
        self.assertFalse((self.orch / "queue").exists())
        back = queue.release(AGENT, r)
        self.assertEqual(self.rel(back), ("queue", AGENT, f"{SEQ:06d}.{cid}.json"))
        self.assertEqual(queue.lane_specs(AGENT), [back])

    def test_finishing_a_second_spec_into_an_existing_done_dir_works(self):
        first = queue.finish(AGENT, queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=1))))
        second = queue.finish(AGENT, queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=2))))
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(sorted(p.name for p in first.parent.iterdir()),
                         sorted([f"{cid_of(rep=1)}.json", f"{cid_of(rep=2)}.json"]))


class TestLaneOrderIsTheFilenameSort(QueueCase):

    def test_append_takes_max_plus_one_and_front_takes_min_minus_one(self):
        a = queue.enqueue(AGENT, spec(rep=1))
        b = queue.enqueue(AGENT, spec(rep=2))
        c = queue.enqueue(AGENT, spec(rep=3), front=True)
        self.assertEqual([seq_of(p) for p in (a, b, c)], [SEQ, SEQ + 1, SEQ - 1])
        self.assertEqual([queue.spec_cid(p) for p in queue.lane_specs(AGENT)],
                         [cid_of(rep=3), cid_of(rep=1), cid_of(rep=2)])

    def test_release_to_the_front_is_the_next_number_below_the_head(self):
        queue.enqueue(AGENT, spec(rep=1))
        claimed = queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=2)))
        head = queue.release(AGENT, claimed)
        self.assertEqual(seq_of(head), SEQ - 1)
        tail = queue.release(AGENT, queue.claim(AGENT, head), front=False)
        self.assertEqual(seq_of(tail), SEQ + 1)
        self.assertEqual([queue.spec_cid(p) for p in queue.lane_specs(AGENT)],
                         [cid_of(rep=1), cid_of(rep=2)])

    def test_zero_is_a_valid_front_number_and_is_not_renumbered(self):
        one = queue.lane_dir(AGENT) / f"000001.{cid_of(rep=1)}.json"
        one.parent.mkdir(parents=True)
        one.write_text("{}\n")
        p = queue.enqueue(AGENT, spec(rep=2), front=True)
        self.assertEqual(p.name, f"000000.{cid_of(rep=2)}.json")
        self.assertTrue(one.exists())

    def test_a_front_insert_below_zero_renumbers_from_seq_start_in_order(self):
        d = queue.lane_dir(AGENT)
        d.mkdir(parents=True)
        (d / f"000000.{cid_of(rep=1)}.json").write_text("{}\n")
        (d / f"000001.{cid_of(rep=2)}.json").write_text("{}\n")
        p = queue.enqueue(AGENT, spec(rep=3), front=True)
        self.assertEqual(p.name, f"{SEQ - 1:06d}.{cid_of(rep=3)}.json")
        self.assertEqual([q.name for q in queue.lane_specs(AGENT)], [
            f"{SEQ - 1:06d}.{cid_of(rep=3)}.json",
            f"{SEQ:06d}.{cid_of(rep=1)}.json",
            f"{SEQ + 1:06d}.{cid_of(rep=2)}.json",
        ])
        self.assertEqual(len(set(seq_of(q) for q in queue.lane_specs(AGENT))), 3)

    def test_a_name_without_a_sequence_number_never_hides_the_numbered_ones(self):
        # Names that sort before every digit and after them alike: the scan
        # must skip each and keep going, or the next number collides.
        d = queue.lane_dir(AGENT)
        d.mkdir(parents=True)
        (d / f"-restored.{cid_of(rep=1)}.json").write_text("{}\n")
        (d / f"{SEQ:06d}.{cid_of(rep=2)}.json").write_text("{}\n")
        (d / f"{cid_of(rep=3)}.json").write_text("{}\n")
        self.assertEqual(queue._seqs(d), [SEQ])
        p = queue.enqueue(AGENT, spec(rep=4))
        self.assertEqual(seq_of(p), SEQ + 1)


class TestLaneOrderIsNumericNotLexical(QueueCase):
    """A spec name is '<seq>.<cid>.json'; every writer in this module
    zero-pads seq to 6 digits, but admission order must not depend on that
    padding holding — a name written by anything else (an operator's
    reorder script, a hand-restored backup) must still sort by the number
    it names, not by the string."""

    def touch(self, name):
        d = queue.lane_dir(AGENT)
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text("{}\n")
        return p

    def test_an_unpadded_number_past_seq_start_sorts_by_value(self):
        # '999999' has more digits than '100000' but is the larger number;
        # a plain string sort gets this one right by luck. The one that
        # doesn't: '99999' (5 digits, unpadded) vs '100000' (6 digits) —
        # '1' < '9' as the first character, so a raw sort would put the
        # numerically LARGER one first.
        lo = self.touch(f"99999.{cid_of(rep=1)}.json")
        hi = self.touch(f"{SEQ}.{cid_of(rep=2)}.json")
        self.assertEqual(queue.lane_specs(AGENT), [lo, hi])

    def test_three_widths_together_sort_purely_by_number(self):
        a = self.touch(f"7.{cid_of(rep=1)}.json")            # 1 digit
        b = self.touch(f"00099980.{cid_of(rep=2)}.json")     # 8 digits, over-padded
        c = self.touch(f"99999.{cid_of(rep=3)}.json")        # 5 digits, unpadded
        d = self.touch(f"{SEQ:06d}.{cid_of(rep=4)}.json")    # 6 digits, the normal shape
        e = self.touch(f"{SEQ + 1}.{cid_of(rep=5)}.json")    # 6 digits, unpadded (== normal)
        self.assertEqual(queue.lane_specs(AGENT), [a, b, c, d, e])

    def test_a_reorder_that_drops_padding_is_still_admitted_in_the_written_order(self):
        # The exact shape of the bug: reorder 4 specs by hand (as an
        # operator's own script might, without the driver's own :06d),
        # crossing the 99999/100000 boundary in the process.
        for i, rep in enumerate((3, 1, 4, 2)):
            self.touch(f"{99998 + i}.{cid_of(rep=rep)}.json")
        got = [queue.spec_cid(p) for p in queue.lane_specs(AGENT)]
        self.assertEqual(got, [cid_of(rep=r) for r in (3, 1, 4, 2)])

    def test_tmp_prefixed_renumber_scratch_files_sort_by_their_own_number(self):
        # _renumber's own transient shape mid-rename: 'tmp-NNNNNN.cid.json'.
        # A concurrent read during that window must not crash or misorder.
        a = self.touch(f"tmp-000000.{cid_of(rep=1)}.json")
        b = self.touch(f"tmp-000001.{cid_of(rep=2)}.json")
        self.assertEqual(queue.lane_specs(AGENT), [a, b])

    def test_a_nameless_number_sorts_last_not_hidden_mid_lane(self):
        numbered = self.touch(f"{SEQ:06d}.{cid_of(rep=1)}.json")
        bare = self.touch(f"{cid_of(rep=2)}.json")            # no leading digits
        dashed = self.touch(f"-restored.{cid_of(rep=3)}.json")
        self.assertEqual(queue.lane_specs(AGENT)[0], numbered)
        self.assertEqual({p.name for p in queue.lane_specs(AGENT)[1:]},
                          {bare.name, dashed.name})

    def test_next_seq_is_correct_regardless_of_the_widths_already_present(self):
        # _seqs parses every name as an int already (unaffected by the
        # ordering bug), but pin it here so a future refactor of _dir_specs
        # can't quietly break _next_seq's min/max by coupling to sort order.
        self.touch(f"99999.{cid_of(rep=1)}.json")
        self.touch(f"{SEQ:06d}.{cid_of(rep=2)}.json")
        tail = queue.enqueue(AGENT, spec(rep=3))
        self.assertEqual(seq_of(tail), SEQ + 1)
        head = queue.enqueue(AGENT, spec(rep=4), front=True)
        self.assertEqual(seq_of(head), 99998)


class TestEnqueueNamesTheCell(QueueCase):

    def test_the_filename_carries_the_specs_own_task_and_rep(self):
        p = queue.enqueue(AGENT, spec(rep=7, task="T2"))
        self.assertEqual(queue.spec_cid(p), cid_of(rep=7, task="T2"))
        self.assertEqual(queue.spec_cid(p), f"{AGENT}_high_beta_apidocs_T2_r7")

    def test_task_defaults_to_t1_and_rep_to_1_when_the_spec_omits_them(self):
        p = queue.enqueue(AGENT, dict(variant="beta_apidocs"))
        self.assertEqual(queue.spec_cid(p), f"{AGENT}_high_beta_apidocs_T1_r1")

    def test_a_cid_already_pending_or_claimed_is_not_queued_twice(self):
        p = queue.enqueue(AGENT, spec())
        self.assertIsNone(queue.enqueue(AGENT, spec()))
        queue.claim(AGENT, p)
        self.assertIsNone(queue.enqueue(AGENT, spec()))
        self.assertEqual(queue.lane_specs(AGENT), [])

    def test_a_sealed_cell_is_refused_and_the_refusal_names_it_and_why(self):
        cid = cid_of()
        (self.ws / cid).mkdir(parents=True)
        (self.ws / cid / ".sealed").write_text("sealed=x\tverdict=green\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(queue.enqueue(AGENT, spec()))
        self.assertIn(cid, out.getvalue())
        self.assertIn("verdict=green", out.getvalue())
        self.assertEqual(self.everywhere(cid), [])


class TestAParkedLaneIsSkippedButKeepsItsBacklog(QueueCase):

    def test_parking_renames_the_lane_and_lane_dirs_hides_it_by_default(self):
        queue.enqueue(AGENT, spec())
        queue.enqueue("bbb", spec())
        self.assertEqual(queue.park_lane(AGENT), "parked")
        self.assertEqual(queue.lane_dirs(), [self.orch / "queue" / "bbb"])
        self.assertEqual(queue.lane_dirs(include_parked=True), [
            self.orch / "queue" / f"{AGENT}.parked",
            self.orch / "queue" / "bbb",
        ])
        self.assertEqual(queue._parked_queues(),
                         [self.orch / "queue" / f"{AGENT}.parked"])
        self.assertEqual(queue.lane_specs(AGENT), [])
        self.assertTrue(queue.lane_has(AGENT, cid_of()))

    def test_a_stray_file_under_queue_is_not_a_lane(self):
        queue.enqueue(AGENT, spec())
        queue.park_lane(AGENT)
        (self.orch / "queue" / "notes.txt").write_text("")
        (self.orch / "queue" / "old.parked").write_text("")
        self.assertEqual(queue.lane_dirs(), [])
        self.assertEqual(queue.lane_dirs(include_parked=True),
                         [self.orch / "queue" / f"{AGENT}.parked"])
        self.assertEqual(queue._parked_queues(),
                         [self.orch / "queue" / f"{AGENT}.parked"])

    def test_enqueue_and_release_land_in_the_parked_dir_and_unpark_restores_order(self):
        queue.enqueue(AGENT, spec(rep=1))
        claimed = queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=2)))
        queue.park_lane(AGENT)
        added = queue.enqueue(AGENT, spec(rep=3))
        back = queue.release(AGENT, claimed)
        parked = ("queue", f"{AGENT}.parked")
        self.assertEqual(self.rel(added)[:2], parked)
        self.assertEqual(self.rel(back)[:2], parked)
        self.assertEqual(queue.lane_specs(AGENT), [])
        self.assertEqual(queue.unpark_lane(AGENT), "resumed")
        self.assertEqual([queue.spec_cid(p) for p in queue.lane_specs(AGENT)],
                         [cid_of(rep=2), cid_of(rep=1), cid_of(rep=3)])

    def test_park_and_unpark_report_the_states_they_find(self):
        self.assertEqual(queue.park_lane(AGENT), "empty")
        self.assertEqual(queue.unpark_lane(AGENT), "not-paused")
        queue.enqueue(AGENT, spec())
        self.assertEqual(queue.park_lane(AGENT), "parked")
        self.assertEqual(queue.park_lane(AGENT), "already")
        queue.lane_dir(AGENT).mkdir()
        self.assertEqual(queue.unpark_lane(AGENT), "conflict")


class TestShelveKeepsARestorableBackup(QueueCase):

    STAMP = re.compile(r"^stop-(\d{8}-\d{6})\.(.+)$")

    def test_the_backup_is_the_original_name_behind_why_and_a_utc_stamp(self):
        p = queue.enqueue(AGENT, spec())
        before = datetime.now(timezone.utc).replace(microsecond=0)
        dest = queue.shelve(p, "stop")
        self.assertEqual(self.rel(dest)[:1], ("backups",))
        m = self.STAMP.match(dest.name)
        self.assertIsNotNone(m, dest.name)
        self.assertEqual(m.group(2), p.name)
        stamped = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        self.assertLessEqual(before, stamped)
        self.assertLess((stamped - before).total_seconds(), 60)
        self.assertFalse(p.exists())
        self.assertEqual(queue.read_spec(dest), spec())

    def test_shelving_from_every_state_lands_beside_the_others(self):
        q = queue.enqueue(AGENT, spec(rep=1))
        r = queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=2)))
        d = queue.finish(AGENT, queue.claim(AGENT, queue.enqueue(AGENT, spec(rep=3))))
        dests = [queue.shelve(p, "stop") for p in (q, r, d)]
        self.assertEqual({x.parent for x in dests}, {self.orch / "backups"})
        for cid in (cid_of(rep=1), cid_of(rep=2), cid_of(rep=3)):
            self.assertEqual(len(self.everywhere(cid)), 1)
            self.assertEqual(self.everywhere(cid)[0][0], "backups")
        self.assertEqual(queue.lane_specs(AGENT), [])
        self.assertEqual(queue.running_specs(), [])


if __name__ == "__main__":
    unittest.main()
