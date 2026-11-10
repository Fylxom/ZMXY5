import unittest

from zmxy import combo

SKILL = {
    "1": {"id": 1, "name": "甲", "actionName": "skill1", "skillDefault": 2.0, "skillAdd": 0.1, "skillAddLevelMax": 5, "skillCountAdd": "10;20;30", "type": 7,
          "atkMax": 3, "cd": "0;4;4;4", "consumeMP": "0;5;5;5", "releaseAddPowerNum": 20,
          "param": {"s": [{"hasDamage": True, "atkMax": 2, "shootFrame": 3, "attackedNumShareGroup": 9},
                          {"hasDamage": True, "atkMax": 2, "shootFrame": 6, "attackedNumShareGroup": 9,
                           "shootTime": 0.5, "next": {"hasDamage": True, "atkMax": 4, "attackedNumShareParent": True}},
                          {"hasDamage": True, "atkMax": 4, "shootFrame": 9}, {"type": 5, "shootFrame": 0, "shootTime": 1.0}]}},
    str(combo.ATTACK): {"id": combo.ATTACK, "name": "普攻", "actionName": "attack1", "skillDefault": 1.0, "skillAdd": 0, "skillCountAdd": "0", "type": 0,
                        "atkMax": 1, "cd": 0, "consumeMP": 0, "param": {"s": [{"hasDamage": True, "atkMax": 1, "shootFrame": 3}]}},
}
ROLE = {"2098": {"skills": "1", "custom": '{"s":{"midTakeBySKill":[["skill3",2035]],"power":{"max":[0,20,50,70,90],"sub":[[2035,[-5,-15,-40,-55,-70]]]},"powerMax":[50,100]}}'}}
EXP = {"10": {"criticalMax": 1000, "luckyMax": 1000, "toughnessMax": 1000, "guardianMax": 1000}}


def sim(resist=0.0, critical=0.0, lucky=0.0):
    me = combo.Me(level=10, atk=1100, critical=critical, lucky=lucky, base_critical=0, base_lucky=0, skills={1: 3}, bar=[1], passive=0, ult=0)
    boss = combo.Boss("靶子", 10, 100, 0, 0, {"windResist": resist})
    return combo.Sim(me, boss, combo.Tables(SKILL, {}, ROLE, {}, EXP))


class ComboTest(unittest.TestCase):
    def test_strikes_merge_shared_boxes(self):
        # 同一组的两个框和接着父框的那一段只算一份；不同组的另算
        self.assertEqual([(round(at, 2), cfg, hits) for at, cfg, hits in combo.strikes(SKILL["1"])], [(0.1, 1, 2), (0.3, 1, 4)])
        self.assertEqual(combo.cast_seconds(SKILL["1"]), 1.0)

    def test_one_hit_follows_client_formula(self):
        # (1100 - 100) × (2.0 + 0.1 × 2) + 第 3 级的固定值 30
        self.assertAlmostEqual(sim().hit(1, 3, 1), 2230)
        self.assertAlmostEqual(sim(resist=-10).hit(1, 3, 1), 2230 * 1.1)      # 抗性被降 10 → 多打一成
        self.assertAlmostEqual(sim(resist=100).hit(1, 3, 1), 2230 * 0.5)      # 抗性 100 → 减半
        self.assertAlmostEqual(sim(critical=500, lucky=500).hit(1, 3, 1), 2230 * (1 + 0.5 * 1.5))  # 一半暴击，暴击多 1.5 倍
        self.assertAlmostEqual(sim().hit(1, 3, 1, weak=-20), 2230 * 1.2)      # 易伤 20

    def test_run_caps_hits_and_respects_cooldown(self):
        res = sim().run(["甲"], 10, fill=False)
        self.assertEqual(res.casts, {"甲": 3})            # 第 0、4、8 秒
        self.assertAlmostEqual(res.total, 2230 * 3 * 3)   # 一招 6 下，按技能表的 3 下封顶
        filled = sim().run(["甲"], 10)
        self.assertGreater(filled.total, res.total)       # 冷却时补普攻
        self.assertIn("普攻", filled.casts)

    def test_round_stops_when_mana_cannot_pay_dearest_skill(self):
        s = sim()
        s.me.mp_max, s.me.re_mp = 12.0, 0.0     # 一次 5 点蓝：放两次剩 2，不够第三次
        res = s.run(["甲"], 100, mana=True, priority=True)
        self.assertEqual(res.casts["甲"], 2)
        self.assertEqual(res.mp_left, 2.0)
        self.assertLess(res.seconds, 100)

    def test_cycle_rests_on_normal_attacks_after_ult(self):
        skill = {**SKILL, "2": {**SKILL["1"], "id": 2, "name": "无双", "actionName": "skillXp2", "cd": 0, "consumeMP": 0, "releaseAddPowerNum": 0}}
        me = combo.Me(level=10, atk=1100, critical=0, lucky=0, base_critical=0, base_lucky=0, skills={1: 3}, bar=[1], passive=0, ult=2)
        s = combo.Sim(me, combo.Boss("靶子", 10, 100, 0, 0, {}), combo.Tables(skill, {}, ROLE, {}, EXP))
        res = s.run(["甲"], 12, cycle=True, xp=combo.XP_FULL, log=True)
        # 怒气是满的：先放无双，然后只普攻，等甲转好（它还没放过，立刻就算转好）再放甲
        self.assertIn("无双，", res.log[0])
        self.assertEqual(res.casts["无双"], 1)
        self.assertEqual(len(res.cycles), 2)

    def test_no_xp_for_15_seconds_after_ult(self):
        skill = {**SKILL, "2": {**SKILL["1"], "id": 2, "name": "无双", "actionName": "skillXp2", "cd": 0, "consumeMP": 0, "releaseAddPowerNum": 0}}
        me = combo.Me(level=10, atk=1100, critical=0, lucky=0, base_critical=0, base_lucky=0, skills={1: 3}, bar=[1], passive=0, ult=2)
        s = combo.Sim(me, combo.Boss("靶子", 10, 100, 0, 0, {}), combo.Tables(skill, {}, ROLE, {}, EXP))
        skill["1"] = {**skill["1"], "addXP": 5000, "addXPMax": 100000}   # 甲打两下就能攒满
        # 开打怒气是满的：不管之后打中多少下，15 秒内放不出第二次无双；过了 15 秒才又攒得起来
        res = s.run(["无双", "甲"], combo.XP_LOCK - 1, xp=combo.XP_FULL, priority=True)
        self.assertEqual(res.casts["无双"], 1)
        self.assertEqual(s.run(["无双", "甲"], combo.XP_LOCK + 5, xp=combo.XP_FULL, priority=True).casts["无双"], 2)


if __name__ == "__main__":
    unittest.main()
