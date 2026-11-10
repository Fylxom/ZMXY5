"""新表体检：``basic/`` 里最新一版数据表，和测试快照那一版（``tests/tables/``）相比，程序依赖的东西还在不在。

全部测试里只有这个文件读 ``basic/``、结果会随游戏每周更新变。它失败 = 这周的更新动了程序读的表：
看报出来的是哪张表哪个字段，代码用到了就改代码；确认没用到就记进下面的 ``SEEN``。
其余测试只读快照，失败只可能是代码改坏了。``basic/`` 里没有比快照新的表时跳过。
"""

import json
import re
import unittest

from tests import TABLES
from zmxy import data, schema, state

# 看过、确认代码没用到的结构变化（原样抄报出来的那句话）。换快照（tools/test_tables.py）后清空。
SEEN: set[str] = set()


def named(folder) -> dict:
    return {re.sub(r"^\d+_", "", p.stem).lower(): p for p in folder.glob("*.json") if re.match(r"\d+_.", p.name)}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class NewTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.latest = data.latest_root()
        if not cls.latest.is_dir() or cls.latest.name <= TABLES.name:
            raise unittest.SkipTest("basic/ 里没有比测试快照新的数据表")
        cls.new = named(cls.latest)

    def test_growth_week_is_still_recognized(self):
        """四个养成活动还在、能认出这一版是第几周（留次数、斗神保护次数、时光之舟提醒都靠它）。"""
        self.assertIsNotNone(state.growth_table_week(list(load(self.new["activity"]).values())), self.latest.name)

    def test_weekly_fairy_scrolls_are_still_on_sale(self):
        """妖幡卷商店认的 9 个周限商品（8022–8030）还在、价格货币和限购没变、没下架。"""
        from unittest import mock
        from zmxy.tasks import fairy_flag_shop
        shop = load(self.new["shop"])
        with mock.patch.object(state, "row", lambda table, key: shop.get(str(key)) if table == "Shop" else None):
            self.assertEqual([i for i, _ in fairy_flag_shop.catalog()], list(fairy_flag_shop.ITEM_IDS), self.latest.name)

    def test_tables_the_code_reads_keep_their_structure(self):
        """快照里有的表（= 测试读到的表）在新版里都还在，原来每一行都有的字段一个没少。"""
        problems = []
        for key, old in sorted(named(TABLES).items()):
            name = re.sub(r"^\d+_", "", old.stem)
            if key not in self.new:
                problems.append(f"{name}：表没了")
            else:
                problems += schema.breaking(name, load(old), load(self.new[key]))
        problems = [x for x in problems if x not in SEEN]
        self.assertEqual(problems, [], f"{self.latest.name} 相比 {TABLES.name}：代码用到了就改代码，没用到就记进 SEEN")


if __name__ == "__main__":
    unittest.main()
