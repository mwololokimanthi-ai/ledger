"""Unit tests for the ledger binding ladder.

Run: python3 -m unittest discover -s tests -v

The ladder is the part worth testing: it decides which transcript a live
process owns, and every branch is a heuristic that must degrade honestly
rather than guess.
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ledger_core as lc  # noqa: E402


def proc(start, cwd="/work/a"):
    return {"start": start, "cwd": cwd}


def trans(sid, started_at, cwd="/work/a", mtime=None):
    return {
        "id": sid,
        "cwd": cwd,
        "started_at": started_at,
        "mtime": time.time() if mtime is None else mtime,
    }


class BindTests(unittest.TestCase):
    def setUp(self):
        # Keep the ladder pure: no launches file, no tmux.
        self._orig_launches = lc.load_launches
        lc.load_launches = lambda: {}

    def tearDown(self):
        lc.load_launches = self._orig_launches

    def test_exact_binds_within_window(self):
        p = {"1": proc(1000)}
        t = [trans("s1", 1060)]
        out = lc.bind(p, t)
        self.assertEqual(out["1"]["sid"], "s1")
        self.assertEqual(out["1"]["kind"], "exact")

    def test_exact_rejects_outside_window(self):
        p = {"1": proc(1000)}
        t = [trans("s1", 500)]  # born 500s before the process
        out = lc.bind(p, t)
        self.assertNotIn("1", out)

    def test_succession_rebinds_after_compaction(self):
        now = time.time()
        p = {"1": proc(1000)}
        stale = trans("old", 1010, mtime=now - 600)
        fresh = trans("new", 1400, mtime=now - 30)
        out = lc.bind(p, [stale, fresh])
        self.assertEqual(out["1"]["sid"], "new")
        self.assertEqual(out["1"]["kind"], "succession")

    def test_created_after_binds_new_file(self):
        p = {"1": proc(1000)}
        t = [trans("s1", 1300)]  # no exact pair, born during the lifetime
        out = lc.bind(p, t)
        self.assertEqual(out["1"]["sid"], "s1")
        self.assertEqual(out["1"]["kind"], "created-after")

    def test_unbound_when_nothing_matches(self):
        p = {"1": proc(1000, cwd="/work/a")}
        t = [trans("s1", 1300, cwd="/work/b")]
        out = lc.bind(p, t)
        self.assertNotIn("1", out)

    def test_resolve_by_row_number(self):
        state = {"sessions": [
            {"id": "a1", "tmux": "-", "tmux_session": "", "intent": "first", "project": "p1", "mtime_epoch": 1},
            {"id": "b2", "tmux": "-", "tmux_session": "", "intent": "second", "project": "p2", "mtime_epoch": 2},
        ]}
        self.assertEqual(lc.resolve(state, "2")["id"], "b2")

    def test_resolve_by_id_prefix(self):
        state = {"sessions": [
            {"id": "a1b2c3", "tmux": "-", "tmux_session": "", "intent": "x", "project": "p", "mtime_epoch": 1},
        ]}
        self.assertEqual(lc.resolve(state, "a1b2")["id"], "a1b2c3")


if __name__ == "__main__":
    unittest.main()
