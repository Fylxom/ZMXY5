"""日常类任务（灵宠采集、幻兽试炼、神秘商店、黑市、聚灵池、善法堂、妖王帖、许愿池、幻兽喂养、联盟仓库）。全部离线。"""

import unittest
from unittest import mock

from zmxy import core, login, protocol as p
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from tests.support import FakeConn, day, run_daily as run


class PetCollectionTests(unittest.TestCase):
    def slot(self, reward, finish, pet="e1"):
        return {"reward": reward, "collectId": 5, "gear": 1, "petEntityId": pet, "finishTime": finish}

    def test_claims_then_redispatches_same_config(self):
        conn = FakeConn({
            "scene.petHandler.getCollectReward": {"collectData": {"0": self.slot(1, 0)}},
            "scene.petHandler.collect": {"collectData": {"0": self.slot(0, 9_999_999)}},
        })
        r = run("pet_collection", conn, {"collectData": {"0": self.slot(0, 100)}})
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.petHandler.getCollectReward"), [{"pos": 0, "double": 0, "must": 0}])
        self.assertEqual(conn.bodies("scene.petHandler.collect"), [{"collectId": 5, "entityId": "e1", "gear": 1, "pos": 0}])

    def test_photo_is_reported_and_full_album_prompts_switch(self):
        ids = ["49", "50", "51", "52", "53", "54"]
        slot = {"reward": 0, "collectId": 101003008, "gear": 1, "petEntityId": "e1", "finishTime": 100}
        conn = FakeConn({
            "scene.petHandler.getCollectReward": {"photoId": 54, "petAlbum": {i: {"time": 1} for i in ids},
                                                  "collectData": {"0": {**slot, "reward": 1}}},
            "scene.petHandler.collect": {"collectData": {"0": {**slot, "finishTime": 9_999_999}}},
        })
        info = {"collectData": {"0": slot}, "petAlbum": {i: {"time": 1} for i in ids[:5]}}
        r = run("pet_collection", conn, info)
        self.assertEqual(r.status, "submitted")
        self.assertIn("获得 1 张相片", r.detail)
        self.assertIn("已集齐，请切换采集材料", r.detail)
        self.assertIn("已集齐，请切换采集材料", r.todo)  # 另记一行“待处理”，状态仍是已完成

    def test_incomplete_album_has_no_switch_prompt(self):
        slot = {"reward": 0, "collectId": 101003008, "gear": 1, "petEntityId": "e1", "finishTime": 2_000_000}
        r = run("pet_collection", FakeConn(), {"collectData": {"0": slot}, "petAlbum": {"49": {}}})
        self.assertNotIn("切换", r.detail)
        self.assertEqual(r.todo, "")

    def test_unfinished_is_left_alone(self):
        conn = FakeConn()
        r = run("pet_collection", conn, {"collectData": {"0": self.slot(0, 2_000_000)}})
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_missing_data_or_time_blocks(self):
        self.assertEqual(run("pet_collection", FakeConn(), {}).status, "blocked")
        s_conn = FakeConn()
        s = Session(p.ClientConfig(uid=1, username="u"))
        s._conn, s.info, s._link = s_conn, {"collectData": {"0": self.slot(0, 1)}}, login.Link(s_conn, s.config, [], {})
        s.bind(load_tasks()["pet_collection"])
        self.assertEqual(load_tasks()["pet_collection"].run(TaskContext(s)).status, "blocked")
        self.assertEqual(s_conn.sent, [])

    def test_unconfirmed_claim_does_not_redispatch(self):
        conn = FakeConn({"scene.petHandler.getCollectReward": {}})
        r = run("pet_collection", conn, {"collectData": {"0": self.slot(0, 100)}})
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("scene.petHandler.collect", conn.routes())


class PetElfTests(unittest.TestCase):
    def info(self, vip=6, used=0):
        return {"vip": {"level": vip}, **day(petElfLottery={"1": used})}

    def test_uses_free_draw(self):
        conn = FakeConn()
        self.assertEqual(run("pet_elf", conn, self.info()).status, "submitted")
        self.assertEqual(conn.sent, [("scene.petHandler.petElfLottery", {"id": 1})])

    def test_used_up_or_unknown(self):
        self.assertEqual(run("pet_elf", FakeConn(), self.info(used=1)).status, "skipped")
        conn = FakeConn()
        self.assertEqual(run("pet_elf", conn, {"vip": {"level": 6}}).status, "blocked")
        self.assertEqual(conn.sent, [])


class ShanfaTests(unittest.TestCase):
    def test_likes_top_of_rank(self):
        conn = FakeConn({"scene.rankHandler.getRank": {"rows": [{"id": "123_4_c0"}]}})
        r = run("shanfa_like", conn, day(criticiseTop1=0))
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.userHandler.criticiseTop1Like"), [{"uid": "123", "type": "role_8"}])

    def test_no_free_left_sends_nothing(self):
        conn = FakeConn()
        self.assertEqual(run("shanfa_like", conn, day(criticiseTop1=1)).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_unknown_count_blocks(self):
        self.assertEqual(run("shanfa_like", FakeConn(), {}).status, "blocked")

    def test_like_timeout_is_uncertain(self):
        conn = FakeConn({"scene.rankHandler.getRank": {"rows": [{"uid": 9}]}})
        orig = conn.request

        def request(route, body):
            if route.endswith("Like"):
                raise TimeoutError("t")
            return orig(route, body)

        conn.request = request
        with self.assertRaises(UncertainOutcome):
            run("shanfa_like", conn, day(criticiseTop1=0))


@mock.patch.object(Session, "refresh", lambda s: s.info)  # 任务开头重新进角：读回来和登录时一样
class MonsterCardTests(unittest.TestCase):
    def info(self, used=0, items=None, vip=0):
        return {"vip": {"level": vip}, "bag": {"itemBagMax": 10, "item": items if items is not None else []},
                **day(monsterCardLottery={"freeNum_1": used})}

    def test_runs_free_count_only(self):
        conn = FakeConn()
        r = run("monster_card", conn, self.info())
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.sent, [("scene.monsterCardHandler.lottery", {"id": 1})])

    def test_free_used_up(self):
        conn = FakeConn()
        self.assertEqual(run("monster_card", conn, self.info(used=1)).status, "skipped")
        self.assertEqual(conn.sent, [])

    def test_missing_bag_blocks(self):
        info = self.info()
        del info["bag"]
        conn = FakeConn()
        self.assertEqual(run("monster_card", conn, info).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_malformed_nested_counters_block_without_request(self):
        for value in ("bad", {"freeNum_1": "bad"}, {"limit_1": -1}):
            with self.subTest(value=value):
                info = self.info()
                info["everyDaySetting"]["monsterCardLottery"] = value
                conn = FakeConn()
                self.assertEqual(run("monster_card", conn, info).status, "blocked")
                self.assertEqual(conn.sent, [])


class BlackMarketTests(unittest.TestCase):
    def shelf(self, item="201001", sold=0):  # 201001：灵魂标价（见 BlackMarket 表）；值是已售数量，非 0 客户端就不给买
        return {"blackMarket": {"data": {item: sold}}}

    def test_buys_first_spirit_item_and_refreshes_free_times(self):
        conn = FakeConn({
            "scene.shopHandler.getBlackMarket": self.shelf(),
            "scene.shopHandler.buyBlackMarket": {"spirit": 60000},
            "scene.shopHandler.freeForceRefreshBlackMarket": self.shelf(),
        })
        r = run("black_market", conn, {"vip": {"level": 6}, "spirit": 100000, **day(freeblackMarketNum=0)})
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.shopHandler.buyBlackMarket")[0], {"shopId": 201001})
        self.assertEqual(len(conn.bodies("scene.shopHandler.freeForceRefreshBlackMarket")), 2)  # VIP6 免费 2 次

    def test_sold_item_is_not_bought(self):
        conn = FakeConn({"scene.shopHandler.getBlackMarket": self.shelf(sold=1)})
        r = run("black_market", conn, {"vip": {"level": 0}, "spirit": 10 ** 9, **day()})
        self.assertNotIn("scene.shopHandler.buyBlackMarket", conn.routes())
        self.assertIn("已售出", r.detail)

    def test_ticket_priced_item_is_never_bought(self):
        conn = FakeConn({"scene.shopHandler.getBlackMarket": self.shelf("203008")})
        r = run("black_market", conn, {"vip": {"level": 0}, "spirit": 10 ** 9, **day()})
        self.assertNotIn("scene.shopHandler.buyBlackMarket", conn.routes())
        self.assertNotEqual(r.status, "submitted")

    def test_paid_refresh_route_is_unreachable(self):
        _, ctx_task = None, load_tasks()["black_market"]
        self.assertNotIn("scene.shopHandler.forceRefreshBlackMarket", ctx_task.reads | ctx_task.writes)


class MysteryShopTests(unittest.TestCase):
    def test_missing_or_invalid_spirit_blocks_purchase(self):
        shelf = {"mysteryShop": {"stall": {"5": 105001}, "count": 4}}
        for value in (None, "broken", -1):
            with self.subTest(spirit=value):
                conn = FakeConn({"scene.shopHandler.getMysteryShop": shelf})
                result = run("mystery_shop", conn, {"vip": {"level": 0}, "spirit": value})
                self.assertEqual(result.status, "blocked")
                self.assertNotIn("scene.shopHandler.buyMysteryShop", conn.routes())

    def test_buys_highest_stage_until_quota(self):
        shelf = {"mysteryShop": {"stall": {"1": 101001, "5": 105001}, "count": 4}}
        conn = FakeConn({
            "scene.shopHandler.getMysteryShop": shelf,
            "scene.shopHandler.buyMysteryShop": {"spirit": 1, "mysteryShop": {"stall": {"1": 101001, "5": 105001}, "count": 5}},
        })
        r = run("mystery_shop", conn, {"vip": {"level": 0}, "spirit": 10 ** 7})  # VIP0 共 5 次，已用 4
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.shopHandler.buyMysteryShop"), [{"stallNum": 5}])

    def test_unknown_count_blocks(self):
        conn = FakeConn({"scene.shopHandler.getMysteryShop": {"mysteryShop": {"stall": {"5": 105001}}}})
        self.assertEqual(run("mystery_shop", conn, {"vip": {"level": 0}, "spirit": 10 ** 7}).status, "blocked")
        self.assertNotIn("scene.shopHandler.buyMysteryShop", conn.routes())


@mock.patch.object(Session, "refresh", lambda s: s.info)  # 选了幻兽才重新进角：读回来和登录时一样
class FairyFeedTests(unittest.TestCase):
    OPT = {"fairy_p1": "f1"}

    def info(self, used=0, level=5, spirit=10 ** 6, role=10):
        return {"vip": {"level": 0}, "spirit": spirit, **day(feedDayNum_0=used),
                "p1": {"level": role, "equipments": {"fairy": {"entityId": "f1", "id": 190070001, "step": 1, "level": level}}}}

    def test_feeds_selected_fairy_until_free_count_used(self):
        def up(body):
            return {"fairy": {"entityId": "f1", "level": body["feedId"] + 1, "exp": 0}}

        conn = FakeConn({"scene.fairyHandler.upgrade": up})
        r = run("fairy_feed", conn, self.info(), self.OPT, now=0)
        self.assertEqual(r.status, "submitted")
        self.assertTrue(1 <= len(conn.sent) <= 10 + 3)
        self.assertEqual(conn.sent[1][1]["feedId"], 6)  # 按响应里的新等级继续
        self.assertEqual(conn.sent[0][1], {"entityId": "f1", "feedId": 5, "index": 0})

    def test_no_selection_reads_list_and_sends_nothing(self):
        conn = FakeConn()
        r = run("fairy_feed", conn, self.info())
        self.assertEqual((r.status, conn.sent), ("skipped", []))
        self.assertEqual([f["entityId"] for f in r.profile["fairy_feed_owned"]], ["f1"])
        self.assertEqual(r.profile["fairy_feed_owned"][0]["index"], 0)

    def test_selected_fairy_must_still_exist(self):
        conn = FakeConn()
        r = run("fairy_feed", conn, self.info(), {"fairy_p1": "gone"})
        self.assertEqual((r.status, conn.sent), ("blocked", []))

    def test_no_spirit_balance_blocks(self):
        info = self.info()
        del info["spirit"]
        conn = FakeConn()
        self.assertEqual(run("fairy_feed", conn, info, self.OPT).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_at_role_level_not_fed(self):
        conn = FakeConn()
        run("fairy_feed", conn, self.info(level=10, role=10), self.OPT)
        self.assertEqual(conn.sent, [])

    def test_missing_daily_object_blocks(self):
        info = self.info()
        del info["everyDaySetting"]
        conn = FakeConn()
        self.assertEqual(run("fairy_feed", conn, info, self.OPT).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_dialog_items_come_from_saved_profile(self):
        pk = load_tasks()["fairy_feed"].choices.picks
        acc = {"profile": {"fairy_feed_owned": [{"entityId": "a", "name": "x", "step": 2, "level": 9, "index": 0},
                                                 {"entityId": "b", "name": "y", "step": 1, "level": 3, "index": 1}]}}
        self.assertEqual([v for v, _ in pk[0].items(acc)], ["a"])
        self.assertEqual([v for v, _ in pk[1].items(acc)], ["b"])
        self.assertEqual(pk[0].items({}), [])


class FairyTrialTests(unittest.TestCase):
    def test_claims_due_slot(self):
        slots = {"0": {"fairy": ["a", "b"], "expiredTime": 10, "id": 1}, "1": {"fairy": [], "expiredTime": 0, "id": 2}, "2": {"fairy": [], "expiredTime": 0, "id": 3}}
        emptied = {"fairyCollectData": {"0": {"fairy": [], "expiredTime": 0, "id": 1}, "1": slots["1"], "2": slots["2"]}}
        conn = FakeConn({"scene.fairyHandler.getCollectReward": emptied})
        r = run("fairy_trial", conn, {"fairyCollectData": slots, **day()})
        self.assertIn("scene.fairyHandler.getCollectReward", conn.routes())
        self.assertEqual(conn.bodies("scene.fairyHandler.getCollectReward"), [{"pos": 0}])
        self.assertIn(r.status, ("submitted", "blocked", "skipped"))

    def test_unconfirmed_claim_stops(self):
        slots = {"0": {"fairy": ["a", "b"], "expiredTime": 10, "id": 1}}
        conn = FakeConn({"scene.fairyHandler.getCollectReward": {}})
        r = run("fairy_trial", conn, {"fairyCollectData": slots, **day()})
        self.assertEqual(r.status, "blocked")
        self.assertEqual(conn.routes(), ["scene.fairyHandler.getCollectReward"])

    def test_missing_data_blocks(self):
        conn = FakeConn()
        self.assertEqual(run("fairy_trial", conn, {}).status, "blocked")
        self.assertEqual(conn.sent, [])

    def test_refresh_only_free_type(self):
        spec = load_tasks()["fairy_trial"]
        slots = {str(i): {"fairy": [], "expiredTime": 0, "id": 999999} for i in range(3)}  # 表里没有的任务 → 无法派遣
        conn = FakeConn({"scene.fairyHandler.refreshCollectTask": {"fairyCollectData": slots}})
        run("fairy_trial", conn, {"fairyCollectData": slots, **day()})
        for body in conn.bodies("scene.fairyHandler.refreshCollectTask"):
            self.assertEqual(body, {"type": 0})
        self.assertLessEqual(len(conn.bodies("scene.fairyHandler.refreshCollectTask")), 2)  # 每日免费刷新 2 次
        self.assertIn("scene.fairyHandler.refreshCollectTask", spec.spends)

    def test_refreshes_when_sends_used_up_and_nothing_fits(self):
        slots = {str(i): {"fairy": [], "expiredTime": 0, "id": 999999} for i in range(3)}
        conn = FakeConn({"scene.fairyHandler.refreshCollectTask": {"fairyCollectData": slots}})
        r = run("fairy_trial", conn, {"fairyCollectData": slots, **day(fairySendDayNum=99)})
        self.assertEqual(conn.routes(), ["scene.fairyHandler.refreshCollectTask"] * 2)
        self.assertIn("今日免费派遣次数已用完", r.detail)

    def test_no_refresh_when_sends_and_refreshes_used_up(self):
        slots = {str(i): {"fairy": [], "expiredTime": 0, "id": 999999} for i in range(3)}
        conn = FakeConn()
        run("fairy_trial", conn, {"fairyCollectData": slots, **day(fairySendDayNum=99, fairyRefreshDayNum=99)})
        self.assertEqual(conn.sent, [])


class WishPoolTests(unittest.TestCase):
    def test_not_enough_spirit_sends_no_wish(self):
        conn = FakeConn({"scene.activityHandler.getWishPoolInfo": {"curKey": "k", "cur": []}})
        r = run("wish_pool", conn, {"everyWeekSetting": {"wish": {"k": {"pos": {}}}}, "spirit": 0})
        self.assertEqual(r.status, "blocked")
        self.assertNotIn("scene.activityHandler.wish", conn.routes())

    def test_missing_cur_key_blocks(self):
        conn = FakeConn()
        self.assertEqual(run("wish_pool", conn, {}).status, "blocked")
        self.assertEqual(conn.routes(), ["scene.activityHandler.getWishPoolInfo"])

    def test_priority_order(self):
        from zmxy.tasks import wish_pool as w

        a = {"pos": 3, "reward": "紫金葫芦", "base_reward": ""}
        b = {"pos": 3, "reward": "未知奖励", "base_reward": ""}
        self.assertLess(w.priority(a), w.priority(b))


class TeacherTests(unittest.TestCase):
    INFO = day(giveLessonCnt=0, personFreeLearnNum=0)

    def test_teach_sends_when_apprentice_and_quota(self):
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"my": {"userdata": {"1": {}}}}})
        r = run("teacher_teach", conn, self.INFO)
        self.assertEqual(r.status, "submitted")
        self.assertIn("scene.apprenticeshipHandler.giveLessons", conn.routes())

    def test_teach_used_up_or_no_apprentice_sends_nothing(self):
        info = day(giveLessonCnt=1)
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"my": {"userdata": {"1": {}}}}})
        self.assertEqual(run("teacher_teach", conn, info).status, "skipped")
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"my": {"userdata": {}}}})
        self.assertEqual(run("teacher_teach", conn, self.INFO).status, "skipped")
        self.assertNotIn("scene.apprenticeshipHandler.giveLessons", conn.routes())

    def test_teach_unknown_daily_state_blocks(self):
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"my": {"userdata": {"1": {}}}}})
        self.assertEqual(run("teacher_teach", conn, {}).status, "blocked")
        self.assertNotIn("scene.apprenticeshipHandler.giveLessons", conn.routes())

    def test_learn_picks_tier_by_total_liking(self):
        master = {"master": {"apprentice": {"1": {"liking": 300}, "2": {"liking": 300}}}}  # 600 > 499 -> 第 2 档
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": master})
        r = run("teacher_learn", conn, self.INFO)
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.apprenticeshipHandler.getBuffer"), [{"type": 0, "id": 2}])

    def test_learn_no_master_skips_and_used_up_skips(self):
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {}})
        self.assertEqual(run("teacher_learn", conn, self.INFO).status, "skipped")
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"master": {"apprentice": {"1": {"liking": 1}}}}})
        self.assertEqual(run("teacher_learn", conn, day(personFreeLearnNum=1)).status, "skipped")
        self.assertNotIn("scene.apprenticeshipHandler.getBuffer", conn.routes())

    def test_learn_bad_liking_blocks(self):
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"master": {"apprentice": {"1": {"liking": "x"}}}}})
        self.assertEqual(run("teacher_learn", conn, self.INFO).status, "blocked")

    def test_write_timeout_is_uncertain(self):
        conn = FakeConn({"scene.apprenticeshipHandler.getApprenticeshipData": {"my": {"userdata": {"1": {}}}}})
        orig = conn.request

        def req(route, body):
            if route.endswith("giveLessons"):
                raise TimeoutError("t")
            return orig(route, body)
        conn.request = req
        with self.assertRaises(UncertainOutcome):
            run("teacher_teach", conn, self.INFO)


class AccountWideTests(unittest.TestCase):
    def setUp(self):  # 固定在中午：00:00–00:15 跑时联盟贡献会真的顺延到 00:15，测试就卡住了
        from datetime import datetime
        from zmxy import servertime
        patch = mock.patch.object(servertime, "clock", return_value=datetime(2026, 11, 9, 12, 0))
        patch.start()
        self.addCleanup(patch.stop)

    def test_account_wide_items_run_before_save_tasks(self):
        ran = []
        acc = lambda name, user, tasks: core.Account(name, p.ClientConfig(uid=1, username=user), tasks)
        with mock.patch("zmxy.runner.run_account", side_effect=lambda a, **k: ran.append((a.name, a.tasks)) or []):
            core.run_batch([acc("a1 · 存档 1", "u", ("pet_elf",)),
                            acc("a1 · 账号级 · 存档 2", "u", ("alliance",))])
        self.assertEqual(ran, [("a1 · 账号级 · 存档 2", ("alliance",)), ("a1 · 存档 1", ("pet_elf",))])

    def test_other_save_of_same_login_skips_account_wide_tasks(self):
        ran = []
        acc = lambda name, user, tasks: core.Account(name, p.ClientConfig(uid=1, username=user), tasks)
        with mock.patch("zmxy.runner.run_account", side_effect=lambda a, **k: ran.append((a.name, a.tasks)) or []):
            out = core.run_batch([acc("a1", "u", ("alliance", "pet_elf")), acc("a2", "u", ("alliance", "pet_elf")),
                                  acc("b", "v", ("alliance",))])
        self.assertEqual([x for x in ran if x[0] != "b"], [("a1", ("alliance", "pet_elf")), ("a2", ("pet_elf",))])
        self.assertIn(("b", ("alliance",)), ran)
        self.assertEqual([r.status for r in out["a2"]], ["skipped"])

    def test_different_logins_run_in_parallel_same_login_in_order(self):
        import threading
        barrier = threading.Barrier(2, timeout=5)  # 两个不同账号必须同时在跑才能通过
        order = []
        acc = lambda name, user: core.Account(name, p.ClientConfig(uid=1, username=user), ("pet_elf",))
        def fake(a, **k):
            if a.name in ("a1", "b1"):
                barrier.wait()
            order.append(a.name)
            return []
        with mock.patch("zmxy.runner.run_account", side_effect=fake):
            out = core.run_batch([acc("a1", "u"), acc("a2", "u"), acc("b1", "v")])
        self.assertLess(order.index("a1"), order.index("a2"))
        self.assertEqual(list(out), ["a1", "a2", "b1"])

    def test_pause_only_stops_same_login(self):
        acc = lambda name, user: core.Account(name, p.ClientConfig(uid=1, username=user), ("pet_elf",))
        ran = []
        def fake(a, **k):
            ran.append(a.name)
            return [core.TaskResult("t", core.PAUSED, "x")] if a.name == "a1" else []
        with mock.patch("zmxy.runner.run_account", side_effect=fake):
            core.run_batch([acc("a1", "u"), acc("a2", "u"), acc("b1", "v")])
        self.assertEqual(sorted(ran), ["a1", "b1"])


class RegistryTests(unittest.TestCase):
    def test_all_tasks_registered_in_current_page_order(self):
        ids = [i for i, spec in load_tasks().items() if not i.startswith("dungeon") and spec.group not in ("周常", "活动")]
        self.assertEqual(ids, ["fulushou", "growth_road", "pet_collection", "fairy_trial", "mystery_shop",
                               "black_market", "pet_elf", "shanfa_like", "monster_card", "wish_pool", "fairy_feed", "union_warehouse",
                               "arena", "pet_arena", "treasure_cave", "quick_develop", "pet_trial", "daily_quest",
                               "nine_tower", "ten_yama", "forest", "wanxiang_tree", "tree_king", "free_maze", "refine",
                               "teacher_teach", "teacher_learn", "alliance", "vip_gift", "month_card", "group_signin"])

    def test_weekly_page_tasks(self):
        weekly = [i for i, spec in load_tasks().items() if spec.group == "周常"]
        self.assertEqual(weekly, ["fairy_flag_shop", "ancient_wilderness", "demon_tower", "last_rewards", "tree_king_nightmare", "star_maze",
                                  "time_boat", "limited_time_shop", "tongtian_consult", "tongtian_hire", "tongtian_escort", "ten_jue", "teacher_trial", "teacher_trial_student", "pet_brawl", "pvp_match", "star_seal", "pet_catch", "pet_gourd_buy", "pet_cultivation", "arena_apply", "pet_arena_apply", "garden", "union_boss", "world_boss", "uid_lookup"])

    def test_purchase_routes_are_declared_as_spends(self):
        from zmxy import guard

        for spec in load_tasks().values():
            for route in spec.reads | spec.writes:
                if any(w in guard._norm(route) for w in guard.PURCHASE_WORDS):
                    self.assertIn(route, spec.spends, f"{spec.id}: {route}")


if __name__ == "__main__":
    unittest.main()


class MonthCardTests(unittest.TestCase):
    ROUTE = "scene.activityHandler.getMonthCardReward"

    def test_claims_with_active_card(self):
        conn = FakeConn({self.ROUTE: {}})
        r = run("month_card", conn, {"monthCard": {"3": {"expiredTime": 2_000_000_000_000}}}, now=1_000_000)
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies(self.ROUTE), [{}])

    def test_expired_or_missing_card_sends_nothing(self):
        for info in ({"monthCard": {"3": {"expiredTime": 5}}}, {"monthCard": {}}):
            conn = FakeConn()
            r = run("month_card", conn, info, now=1_000_000)
            self.assertEqual(r.status, "skipped")
            self.assertEqual(conn.sent, [])

    def test_unknown_time_expiry_or_history_blocks_claim(self):
        valid = {"expiredTime": 2_000_000_000_000}
        cases = [({}, 1_000_000), ({"monthCard": {"3": valid}}, None)]
        cases += [({"monthCard": {"3": {"expiredTime": expiry}}}, 1_000_000)
                  for expiry in (None, "broken", -1)]
        cases += [({"monthCard": {"3": {**valid, "history": history}}}, 1_000_000)
                  for history in (None, [], "broken")]
        cases += [({"monthCard": {"3": valid, "4": malformed}}, 1_000_000)
                  for malformed in (None, [], "broken")]
        for info, now in cases:
            with self.subTest(info=info, now=now):
                conn = FakeConn()
                result = run("month_card", conn, info, now=now)
                self.assertEqual((result.status, conn.sent), ("blocked", []))

    def test_already_claimed_today_is_skipped(self):
        conn = FakeConn()
        card = {"expiredTime": 2_000_000_000_000, "history": {"1970/1/12": 1}}  # now=1_000_000 秒 = 1970/1/12
        r = run("month_card", conn, {"monthCard": {"3": card}}, now=1_000_000)
        self.assertEqual(r.status, "skipped")
        self.assertEqual(conn.sent, [])
