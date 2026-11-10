"""斗神大会 / 灵宠乱斗开始匹配：逃跑禁赛时交出“过几分钟再来”的占位结果，由执行器先做后面的任务。全部离线。"""

import unittest

from zmxy import dungeon, protocol as p
from zmxy.tasks import match_start as ms

BANNED = "灵宠乱斗开始匹配 失败：你已逃跑累计达到3次，请5分钟后再继续匹配"


class Ctx:
    def __init__(self, reply):
        self.reply = reply

    def request(self, route, body):
        if isinstance(self.reply, str):
            raise p.AllianceError(self.reply)
        return self.reply


class MatchStartTests(unittest.TestCase):
    def test_reads_wait_from_message(self):
        self.assertEqual(ms.escape_wait(BANNED), 5 * 60 + ms.EXTRA_SECONDS)
        self.assertIsNone(ms.escape_wait("今日次数已用完"))

    def test_ban_becomes_retry_result(self):
        with self.assertRaises(ms.Banned) as cm:
            ms.start(Ctx(BANNED), "r", {})
        r = cm.exception.result("pet_brawl", "已打 2 场，今日乱斗币 4/20")
        self.assertEqual((r.status, r.pending, r.retry_in), ("skipped", True, 320))
        self.assertEqual(r.detail, f"已打 2 场，今日乱斗币 4/20；{BANNED}；5 分 20 秒后再匹配，先做后面的任务")

    def test_other_rejection_stops(self):
        with self.assertRaises(dungeon.Stop) as cm:
            ms.start(Ctx("今日次数已用完"), "r", {})
        self.assertEqual(str(cm.exception), "开始匹配失败：今日次数已用完")

    def test_match_ok(self):
        self.assertEqual(ms.start(Ctx({"ok": 1}), "r", {}), {"ok": 1})
