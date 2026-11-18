import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.support import Fake, run
from zmxy import servertime
from zmxy.tasks import union_boss as u

HP = 2.9e9
LEAGUE = {"league": {"leagueId": 1}}


class Boss(Fake):
    """开战后 Boss 血量按直线掉（别人在打），我报的伤害也扣进去；掉到 0 推结算。进场应答里没有 Boss 对象（真机就是这样）。"""

    def __init__(self, test, lasts=230.0, wait=60.0, boss=None, hp_push=True):
        super().__init__(self.answer)
        self.test, self.lasts, self.wait, self.boss, self.hp_push = test, lasts, wait, boss, hp_push
        self.mine, self.times, self.shares, self.opens, self.done = 0.0, [], [], None, False

    def answer(self, route, body):
        if route == u.R_ENTER:
            self.opens = self.test.clock + self.wait
            now = servertime.now() * 1000
            out = {"startTime": now + self.wait * 1000, "endTime": now + 900_000, "serverTime": now}
            return {**out, "Boss": self.boss} if self.boss else out
        if route == u.R_HIT:
            self.shares.append(self.hp() / HP)
            self.mine += body["hurt"]
            self.times.append(self.test.clock)
            self.pushes.append(SimpleNamespace(route=u.P_SELF, body={"uid": 1, "hit": self.mine}))
        return None

    def hp(self):
        others = HP * max(0.0, self.test.clock - self.opens) / self.lasts if self.opens is not None and self.test.clock > self.opens else 0.0
        return max(0.0, HP - others - self.mine)

    def drain_pushes(self):
        if self.opens is not None and not self.done:
            hp = self.hp()
            if self.hp_push:
                self.pushes.append(SimpleNamespace(route=u.P_HP, body={"message": {"hp": hp}}))
            if hp <= 0:
                self.done = True
                self.pushes.append(SimpleNamespace(route=u.P_FINISH, body={"hit": self.mine, "hpMax": HP, "rank": 3, "success": True}))
        return super().drain_pushes()


class UnionBossTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0

        def sleep(ctx, seconds, background=True):
            self.clock += seconds
            return not ctx.stopped

        for patch in (mock.patch("zmxy.core.TaskContext.sleep", sleep), mock.patch.object(u.time, "monotonic", lambda: self.clock)):
            patch.start()
            self.addCleanup(patch.stop)

    def fight(self, damage=14, info=LEAGUE, **kw):
        conn = Boss(self, **kw)
        return conn, run(u.ID, conn, info, {u.KEY: damage} if damage else {})

    def test_needs_a_target(self):
        conn, res = self.fight(damage=None)
        self.assertEqual(res.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_needs_a_league(self):
        conn, res = self.fight(info={})
        self.assertEqual(res.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_reaches_the_target_as_the_boss_falls(self):
        """目标 14%：Boss 死的时候打满，不超；第一笔在开战以后；Boss 掉一半时伤害也在一半上下。"""
        conn, res = self.fight()
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertEqual(conn.routes()[0], "challengeBoss")
        self.assertEqual(conn.routes()[-1], "exitLeagueCopy")
        goal = HP * 0.14
        self.assertLessEqual(conn.mine, goal)
        self.assertGreater(conn.mine, goal * 0.97)
        self.assertGreaterEqual(conn.times[0], conn.opens + u.FIRST[0][1])
        self.assertIn("第 3 名", res.detail)

    def test_follows_the_boss_halfway(self):
        seen = []
        conn = Boss(self)
        real = conn.drain_pushes

        def watch():
            if conn.opens is not None and not seen and conn.hp() <= HP / 2:
                seen.append(conn.mine)
            return real()

        conn.drain_pushes = watch
        res = run(u.ID, conn, LEAGUE, {u.KEY: 14})
        self.assertEqual(res.status, "submitted", res.detail)
        half = HP * 0.14 / 2 / u.AHEAD
        self.assertTrue(half * 0.75 <= seen[0] <= half * 1.25, (seen, half))

    def test_reports_like_the_client(self):
        """血量多的时候至少隔 5 秒报一次；Boss 剩 1%–10% 时 1 秒，≤ 1% 时每下都发。"""
        conn, res = self.fight(damage=3.5)
        self.assertEqual(res.status, "submitted", res.detail)
        gaps = [b - a for a, b in zip(conn.times, conn.times[1:])]
        self.assertTrue(all(g >= 1.0 for g, share in zip(gaps, conn.shares) if share > 0.02), gaps)  # ≤ 1% 时每下都发
        self.assertGreaterEqual(sorted(gaps)[len(gaps) // 2], 5.0)

    def test_faster_boss_still_reaches_the_target(self):
        conn, res = self.fight(lasts=120.0)
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertGreater(conn.mine, HP * 0.14 * 0.85)  # 2 分钟就死的 Boss：偶尔差几个百分点

    def test_out_of_range_is_skipped(self):
        conn, res = self.fight(damage=95)
        self.assertEqual(res.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_boss_hp_from_entry_response(self):
        """进场应答带了总血量就按它算。"""
        conn, res = self.fight(damage=10, boss={"hpMax": HP})
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertTrue(HP * 0.095 < conn.mine <= HP * 0.1, conn.mine)

    def test_late_join_uses_highest_hp_seen(self):
        """开战后才进场、没看到满血：按看到过的最高血量算，照样打（会略少一点）。"""
        conn, res = self.fight(damage=10, wait=-30.0)
        self.assertEqual(res.status, "submitted", res.detail)
        self.assertTrue(HP * 0.075 < conn.mine <= HP * 0.1, conn.mine)

    def test_summary(self):
        self.assertEqual(u.summary({u.KEY: 12.5}), "目标 12.5%")
        self.assertEqual(u.summary({u.KEY: 10}), "目标 10%")
        self.assertEqual(u.summary({}), "未设置目标伤害")


if __name__ == "__main__":
    unittest.main()
