"""竞技场 / 斗宠大会报名（定时页）。全部离线。"""

import unittest

from datetime import datetime

from tests.support import CN, Fake, run


def ts(y, m, d, h=12, mi=0, sec=0):
    return int(datetime(y, m, d, h, mi, sec, tzinfo=CN).timestamp())



FRI_AFTER, FRI_BEFORE, SUN_LATE, SUN_AFTER = ts(2026, 10, 9, 16, 0, 1), ts(2026, 10, 9, 15, 59), ts(2026, 10, 11, 21, 59), ts(2026, 10, 11, 22, 1)
WEEK = lambda **kw: {"everyWeekSetting": kw}


class ArenaApplyTests(unittest.TestCase):
    def go(self, tid, info, now):
        conn = Fake()
        return run(tid, conn, info, now=now), conn

    def test_applies_inside_window(self):
        for tid, route in (("arena_apply", "arenaHandler.applyFor"), ("pet_arena_apply", "petArenaHandler.applyFor")):
            for now in (FRI_AFTER, SUN_LATE):
                r, conn = self.go(tid, WEEK(), now)
                self.assertEqual(r.status, "submitted", r.detail)
                self.assertEqual([r.split("scene.")[-1] for r, _ in conn.sent], [route])
                self.assertEqual(conn.bodies("applyFor"), [{"index": 0}])

    def test_arena_picks_player_pet_ignores_it(self):
        for mode, index in ((None, 0), ("1p", 0), ("2p", 1)):
            conn = Fake()
            run("arena_apply", conn, WEEK(), {"mode": mode} if mode else {}, now=FRI_AFTER)
            self.assertEqual(conn.bodies("applyFor"), [{"index": index}])
        conn = Fake()
        run("pet_arena_apply", conn, WEEK(), {"mode": "2p"}, now=FRI_AFTER)
        self.assertEqual(conn.bodies("applyFor"), [{"index": 0}])

    def test_outside_window_sends_nothing(self):
        for now in (FRI_BEFORE, SUN_AFTER, ts(2026, 10, 7, 12, 0)):
            r, conn = self.go("arena_apply", WEEK(), now)
            self.assertEqual((r.status, conn.routes()), ("skipped", []), r.detail)

    def test_already_applied_each_own_flag(self):
        r, conn = self.go("arena_apply", WEEK(applyForArenaTime=1760000000), FRI_AFTER)
        self.assertEqual((r.status, conn.routes()), ("skipped", []))
        r, conn = self.go("pet_arena_apply", WEEK(applyForArenaTime=1760000000), FRI_AFTER)  # 竞技场报了不代表斗宠报了
        self.assertEqual(r.status, "submitted")

    def test_unknown_state_blocks(self):
        for info, now in (({}, FRI_AFTER), (WEEK(), None)):
            r, conn = self.go("arena_apply", info, now)
            self.assertEqual((r.status, conn.routes()), ("blocked", []), r.detail)


if __name__ == "__main__":
    unittest.main()
