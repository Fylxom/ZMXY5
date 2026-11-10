import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from zmxy import globalcfg, store, timeplan


def fake_dpapi(data: bytes, protect: bool) -> bytes:
    return b"X" + data if protect else data[1:]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s" / "settings.json"
        patcher = mock.patch.object(store, "dpapi", fake_dpapi)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_seal_unseal_roundtrip_and_empty(self):
        sealed = store.seal("p@ss")
        self.assertTrue(sealed.startswith(store.PREFIX))
        self.assertNotIn("p@ss", sealed)
        self.assertEqual(store.unseal(sealed), "p@ss")
        self.assertEqual(store.seal(""), "")
        self.assertEqual(store.unseal("plain"), "")  # 不是密文就当没有

    def test_unseal_failure_returns_empty(self):
        with mock.patch.object(store, "dpapi", side_effect=OSError("x")):
            self.assertEqual(store.unseal(store.PREFIX + "AAAA"), "")
        self.assertEqual(store.unseal(store.PREFIX + "!!notbase64!!"), "")

    def test_secrets_stored_separately_and_never_plain_on_disk(self):
        s = store.Store(self.path)
        acc = {"username": "u"}
        s.set_secret(acc, "password", "PW1")
        s.set_secret(acc, "second_password", "PW2")
        s.set_secret(acc, "cookie", "sid=C")
        s.accounts.append(acc)
        s.save()
        raw = self.path.read_text(encoding="utf-8")
        for plain in ("PW1", "PW2", "sid=C"):
            self.assertNotIn(plain, raw)
        again = store.Store(self.path).accounts[0]
        self.assertEqual(store.Store(self.path).get_secret(again, "password"), "PW1")
        self.assertEqual(store.Store(self.path).get_secret(again, "second_password"), "PW2")
        self.assertEqual(store.Store(self.path).get_secret(again, "cookie"), "sid=C")

    def test_undecryptable_secret_keeps_original_ciphertext_on_disk(self):
        s = store.Store(self.path)
        acc = {"username": "u"}
        s.set_secret(acc, "password", "PW1")
        s.accounts.append(acc)
        s.save()
        original = json.loads(self.path.read_text(encoding="utf-8"))["accounts"][0]["password"]
        with mock.patch.object(store, "dpapi", side_effect=OSError("other user")):
            s2 = store.Store(self.path)
            self.assertEqual(s2.get_secret(s2.accounts[0], "password"), "")
            s2.to_account(s2.accounts[0])  # 读取不应改写密文
            s2.save()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["accounts"][0]["password"], original)

    def test_slot_ui_1_to_8_maps_to_protocol_0_to_7(self):
        s = store.Store(self.path)
        for ui, proto in ((1, 0), (3, 2), (8, 7), ("5", 4)):
            self.assertEqual(s.to_account({"username": "u"}, None, ui).config.record_index, proto)
        self.assertEqual(s.to_account({"username": "u"}).config.record_index, 0)  # 默认存档 1

    def test_saves_do_not_share_settings(self):
        s = store.Store(self.path)
        acc = {"username": "u", "name": "n"}
        s.save_cfg(acc, 1).update(tasks=["growth_road"], options={"dungeon_main": {"x": 1}}, no_pickup=["exp"])
        s.save_cfg(acc, 2).update(tasks=["pet_elf"])
        one, two = s.to_account(acc, None, 1), s.to_account(acc, None, 2)
        self.assertEqual((one.tasks, one.no_pickup, one.options, one.name), (("growth_road",), frozenset({"exp"}), {"dungeon_main": {"x": 1}}, "n · 存档 1"))
        self.assertEqual((two.tasks, two.no_pickup, two.options), (("pet_elf",), frozenset(), {}))
        self.assertEqual(s.active_slots(acc), [1, 2])
        self.assertEqual([(a.config.record_index, a.tasks) for a in s.run_accounts(acc)], [(0, ("growth_road",)), (1, ("pet_elf",))])
        self.assertEqual(s.run_accounts(acc, slots=[2])[0].tasks, ("pet_elf",))
        self.assertEqual(s.to_account(acc, None, 3).tasks, ())  # 没设置过的存档是空的，且不会被创建
        self.assertNotIn("3", acc["saves"])

    def test_one_shot_toggles_reset_in_their_own_storage_scope(self):
        s = store.Store(self.path)
        acc = {"username": "u"}
        s.accounts.append(acc)
        s.save_cfg(acc, 2).setdefault("options", {})["task"] = {
            "levels": [1], "skip_gold": True, "other": True,
        }
        s.wide_cfg("u", "world_boss").setdefault("options", {})["world_boss"] = {
            "levels": [2], "skip_gold": True, "mode": "mid",
        }

        self.assertTrue(s.reset_one_shot_toggles(acc, 2, "task", ("skip_gold",)))
        self.assertTrue(s.reset_one_shot_toggles(acc, 2, "world_boss", ("skip_gold",), account_wide=True))
        self.assertFalse(s.reset_one_shot_toggles(acc, 2, "world_boss", ("skip_gold",), account_wide=True))
        s.save()

        loaded = store.Store(self.path)
        saved = loaded.accounts[0]
        self.assertFalse(loaded.save_view(saved, 2)["options"]["task"]["skip_gold"])
        self.assertEqual(loaded.save_view(saved, 2)["options"]["task"], {
            "levels": [1], "skip_gold": False, "other": True,
        })
        self.assertEqual(loaded.wide_options("u", ["world_boss"])["world_boss"], {
            "levels": [2], "skip_gold": False, "mode": "mid",
        })

    def test_account_profile_is_shared_by_saves_and_persisted(self):
        s = store.Store(self.path)
        acc = {"username": "u", "name": "n"}
        s.accounts.append(acc)
        one, two = s.to_account(acc, None, 1), s.to_account(acc, None, 2)

        self.assertIs(one.account_profile, two.account_profile)
        one.account_profile["game_high_jump_score"] = 117
        s.save()

        loaded = store.Store(self.path)
        self.assertEqual(loaded.account_profile("u"), {"game_high_jump_score": 117})

        s.reset_save(acc, 1)
        self.assertEqual(s.account_profile("u"), {"game_high_jump_score": 117})
        s.reset_account(acc)
        self.assertEqual(s.account_profile("u"), {})

    def test_switch_off_clears_only_those_tasks_in_every_save(self):
        s = store.Store(self.path)
        a, b = {"username": "u", "name": "n"}, {"username": "v", "name": "m"}
        s.accounts.extend([a, b])
        s.save_cfg(a, 1).update(tasks=["cloud_ladder", "pet_elf"])
        s.save_cfg(a, 2).update(tasks=["pet_elf"])
        s.save_cfg(b, 3).update(tasks=["mid_autumn_bobing", "cloud_ladder"])
        s.switch_off(["cloud_ladder", "mid_autumn_bobing"])
        self.assertEqual([s.save_view(a, 1)["tasks"], s.save_view(a, 2)["tasks"], s.save_view(b, 3)["tasks"]], [["pet_elf"], ["pet_elf"], []])
        s.save_cfg(a, 2)["tasks"].append("cloud_ladder")
        s.switch_off(["cloud_ladder"], (b, 3))  # 只关一个存档（云梯走到终点）：别的账号/存档不动
        s.switch_off(["cloud_ladder"], (a, 9))  # 没设置过的存档：不出错、不创建
        self.assertEqual((s.save_view(a, 2)["tasks"], "9" in a["saves"]), (["pet_elf", "cloud_ladder"], False))
        s.switch_off(["cloud_ladder"], (a, 2))
        self.assertEqual(s.save_view(a, 2)["tasks"], ["pet_elf"])
        s.save_cfg(a, 1)["tasks"].append("cloud_ladder")  # 不锁定：之后手动打开照样保存、执行
        self.assertEqual(s.to_account(a, None, 1).tasks, ("pet_elf", "cloud_ladder"))

    def test_game_tls_matches_old_client(self):
        c = store.Store(self.path).to_account({"username": "u"}).config
        self.assertFalse(c.tls_verify)
        self.assertEqual(c.tls_server_name, "g2-zm5.4399zmxy.com")

    def test_to_account_defaults_and_cookie_callback(self):
        s = store.Store(self.path)
        cb = mock.Mock()
        a = s.to_account({"username": "u", "saves": {"1": {"tasks": ["alliance"]}}}, cb)
        self.assertEqual(a.name, "u · 存档 1")
        self.assertEqual(a.tasks, ("alliance",))
        self.assertIs(a.on_cookie, cb)

    def test_corrupt_or_wrong_shape_file_falls_back_to_empty(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{not json", json.dumps({"accounts": "x"}), "[]"):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(store.Store(self.path).accounts, [])

    def test_save_is_atomic_and_leaves_no_tmp(self):
        s = store.Store(self.path)
        s.accounts.append({"username": "u"})
        s.save()
        self.assertFalse(self.path.with_suffix(".tmp").exists())
        self.assertEqual(store.Store(self.path).accounts, [{"username": "u"}])

    def test_load_removes_old_pvp_times_across_accounts_and_saves(self):
        expected = {"accounts": [
            {"username": "u", "password": "dpapi:unchanged", "saves": {
                "1": {"tasks": ["pvp_match", "pet_brawl"], "options": {
                    "pvp_match": {"other": 1}, "pet_brawl": {}, "ancient": {"times": 3}},
                    "timers": {"pvp_match": {"on": True, "at": "20:30"}}},
                "8": {"options": {"pvp_match": {}, "pet_brawl": {"other": 2}}}}},
            {"username": "v", "saves": {"2": {"options": {"pet_brawl": {}}}}},
            {"username": "empty"}], "theme": "dark"}
        old = json.loads(json.dumps(expected))
        for acc in old["accounts"]:
            for cfg in acc.get("saves", {}).values():
                for tid in ("pvp_match", "pet_brawl"):
                    if tid in cfg["options"]:
                        cfg["options"][tid]["times"] = "old count"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(old), encoding="utf-8")
        s = store.Store(self.path)
        self.assertEqual(s.data, expected)
        s.save()
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), expected)

    def test_save_never_persists_pvp_times(self):
        s = store.Store(self.path)
        s.accounts.append({"username": "u", "saves": {"1": {"options": {
            "pvp_match": {"times": 0}, "pet_brawl": {"times": 1}, "ancient": {"times": 2}}}}})
        s.save()
        expected = {"pvp_match": {}, "pet_brawl": {}, "ancient": {"times": 2}}
        self.assertEqual(s.accounts[0]["saves"]["1"]["options"], expected)
        self.assertEqual(store.Store(self.path).accounts[0]["saves"]["1"]["options"], expected)


@unittest.skipUnless(sys.platform == "win32", "仅 Windows 有 DPAPI")
class WideTaskTests(unittest.TestCase):
    """账号级任务：按登录账号保存（同账号各存档共用，用户明确要求），存档独立选择，与界面选中的存档无关。"""

    WIDE = {"alliance": True, "vip_gift": False}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = store.Store(Path(self.tmp.name) / "settings.json")
        self.s.accounts[:] = [{"name": "a1", "username": "u", "saves": {"1": {"tasks": ["alliance", "pet_elf"]}}},
                              {"name": "a2", "username": "u", "saves": {"2": {"tasks": ["pet_elf"]}}},
                              {"name": "b", "username": "v"}]

    def run_accounts(self, entries, only=None):
        return self.s.wide_accounts(entries, self.WIDE, lambda a: None, only)

    def test_chosen_save_is_independent_of_selected_entry(self):
        self.s.wide_cfg("u", "alliance", True)["record"] = 5       # 选了 5，界面里根本没有存档 5 的条目
        for entries in ([self.s.accounts[0]], [self.s.accounts[1]]):
            out = self.run_accounts(entries)
            self.assertEqual([(a.config.record_index, a.tasks) for a in out], [(4, ("alliance",))])

    def test_one_run_item_per_login_and_save_and_default_on_only(self):
        out = self.run_accounts(self.s.accounts)                    # vip_gift 默认不执行，a1/a2 同账号只出一份
        self.assertEqual([(a.name, a.tasks) for a in out], [("a1 · 账号级 · 存档 1", ("alliance",)), ("b · 账号级 · 存档 1", ("alliance",))])
        self.s.wide_cfg("u", "vip_gift")["on"] = True
        self.s.wide_cfg("u", "vip_gift")["record"] = 2
        out = self.run_accounts([self.s.accounts[0]])
        self.assertEqual([(a.config.record_index, a.tasks) for a in out], [(0, ("alliance",)), (1, ("vip_gift",))])

    def test_multi_save_picks_the_save_by_weekday(self):
        cfg = self.s.wide_cfg("u", "alliance", True)
        cfg.update(record=5, week=[1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([self.s.wide_record(cfg, d) for d in (0, 6)], [5, 5])       # 开关没开：仍用单个存档
        cfg["multi"] = True
        self.assertEqual([self.s.wide_record(cfg, d) for d in range(7)], [1, 2, 3, 4, 5, 6, 7])  # 周一…周日
        with mock.patch.object(store.Store, "wide_record", staticmethod(lambda c, weekday=None: 3)):
            out = self.run_accounts([self.s.accounts[0]])
        self.assertEqual([(a.name, a.config.record_index) for a in out], [("a1 · 账号级 · 存档 3", 2)])
        cfg["week"] = [1, 2, 99]                                                      # 设置坏了：退回单个存档
        self.assertEqual(self.s.wide_record(cfg, 2), 5)

    def test_reset_account_clears_every_save_and_wide_settings_but_keeps_login(self):
        st = store.Store.__new__(store.Store)
        acc = {"username": "u1", "name": "大号", "saves": {"1": {"tasks": ["a"], "profile": {"lv": 9}}, "3": {"options": {"x": 1}, "resume": ["a"]}}}
        st.data = {"accounts": [acc], "wide": {"u1": {"vip_gift": {"on": True, "record": 3}}, "u2": {"vip_gift": {"on": True, "record": 2}}}}
        st.reset_account(acc)
        self.assertEqual(acc, {"username": "u1", "name": "大号", "saves": {"1": {}, "3": {}}})
        self.assertEqual(st.data["wide"], {"u2": {"vip_gift": {"on": True, "record": 2}}})

    def test_account_wide_tasks_ignore_save_tidy(self):
        """账号级执行项（含单项执行和断点继续）不继承所选存档的同物归库设置。"""
        acc = self.s.accounts[0]
        acc["saves"]["1"]["tidy"] = {"on": True, "every": 0}
        acc["saves"]["2"] = {"tidy": {"on": True, "every": 2}}
        self.s.wide_cfg("u", "vip_gift")["on"] = True
        self.s.wide_cfg("u", "vip_gift")["record"] = 2
        out = self.run_accounts([acc])
        self.assertEqual([(a.tasks, a.tidy) for a in out], [(("alliance",), None), (("vip_gift",), None)])
        self.s.wide_cfg("u", "vip_gift")["record"] = 1
        out = self.run_accounts([acc])
        self.assertEqual([(a.tasks, a.tidy) for a in out], [(("alliance", "vip_gift"), None)])
        self.assertEqual([a.tidy for a in self.run_accounts([acc], only="vip_gift")], [None])
        acc["saves"]["1"]["resume"] = ["alliance"]
        self.assertEqual([a.tidy for a in self.s.resume_accounts(self.WIDE, lambda a: None)], [None])
        acc["saves"]["1"]["resume"] = ["alliance", "vip_gift"]
        self.assertEqual([a.tidy for a in self.s.resume_accounts(self.WIDE, lambda a: None)], [None])
        acc["saves"]["1"]["resume"] = ["pet_elf"]
        resumed = self.s.resume_accounts(self.WIDE, lambda a: None)
        self.assertEqual([(a.tasks, a.tidy) for a in resumed], [(("pet_elf",), 0)])
        self.assertEqual(self.s.to_account(acc, None, 1).tidy, 0)  # 普通存档任务继续使用该存档的设置

    def test_only_ignores_switch(self):
        self.s.wide_cfg("u", "alliance", True)["on"] = False
        out = self.run_accounts([self.s.accounts[0]], only="alliance")
        self.assertEqual([a.tasks for a in out], [("alliance",)])


class TimerTests(unittest.TestCase):
    """按开放时段的定时项（globalcfg 存设置、timeplan 管到点）：每个时段只做一次，到点时选中的存档一起排队，账号级任务只选账号。
    测试用的数据表是 20260928 版：国庆前后通天河每天开放。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = store.Store(Path(self.tmp.name) / "settings.json")
        self.s.accounts[:] = [{"name": "a", "username": "u", "saves": {}}]
        self.s.data["global"] = {
            "pvp_match": {"on": True, "session": "evening", "delay": 5, "accounts": {"u": {"slots": [1, 3]}}},
            "pet_brawl": {"on": False, "session": "both", "delay": 0, "accounts": {"u": {"slots": [1]}}},
            "tongtian_hire": {"on": True, "session": "evening", "delay": 10, "accounts": {"u": {"on": True}}}}

    def cfg(self, tid):
        return self.s.data["global"][tid]

    def at(self, hour, minute, day=8):
        """到点的 [(存档, 任务)]（账号级的存档是 None），和界面排队的展开方式一样。"""
        return [(slot, item) for item in globalcfg.due(self.s, datetime(2026, 10, day, hour, minute))
                for _acc, slots in globalcfg.targets(self.s, item) for slot in slots]

    def test_parse_clock(self):
        self.assertEqual([store.parse_clock(t) for t in ("20:30", "8:05", " 00:00 ", "20：30")], [1230, 485, 0, 1230])
        self.assertEqual([store.parse_clock(t) for t in ("", None, "20:3", "2030", "24:00", "12:60", "a:bc")], [None] * 7)

    def test_windows_from_tables(self):
        self.assertEqual(timeplan.windows("pvp_match", 0), [timeplan.Window(720, 840), timeplan.Window(1140, 1260)])
        self.assertEqual(timeplan.parse_windows("12:00:00-14:00"), [])  # 读不懂就整个不认

    def test_malformed_settings_never_fire(self):
        self.s.data["global"].update({"removed_task": {"on": True, "session": "noon"}, "uid_lookup": {"on": True, "session": "noon"}})
        self.cfg("pvp_match")["on"] = "true"
        self.cfg("tongtian_hire")["accounts"] = {"u": {"on": "yes"}}
        self.assertEqual(self.at(19, 30), [])
        self.assertNotIn("uid_lookup", timeplan.TIMER_IDS)
        self.cfg("pvp_match").update(on=True, accounts={"u": {"slots": [0, 9, True, "2"]}})  # 没有一个是 1–8 的存档
        self.assertEqual(self.at(19, 30, day=9), [])

    def test_fires_once_per_window_after_delay(self):
        self.assertEqual(self.at(19, 4), [])
        self.assertEqual(self.at(19, 5), [(1, "pvp_match"), (3, "pvp_match")])  # 晚上场开场后 5 分钟，选中的存档一起
        self.assertEqual(self.at(19, 9), [])
        self.assertEqual(self.at(19, 10), [(None, "tongtian_hire")])  # 账号级：存档由页面上它自己的设置决定
        self.assertEqual(self.at(20, 0), [])  # 这个时段做过了
        self.assertEqual(self.at(19, 30, day=9), [(1, "pvp_match"), (3, "pvp_match"), (None, "tongtian_hire")])  # 第二天，晚开机也补做

    def test_catch_up_until_window_end(self):
        self.assertEqual(self.at(20, 54), [(1, "pvp_match"), (3, "pvp_match"), (None, "tongtian_hire")])  # 21 点结束前 5 分钟还算
        self.assertEqual(self.at(20, 56, day=9), [(None, "tongtian_hire")])  # 斗神大会来不及了；通天河到 22 点
        self.assertEqual(self.at(21, 56, day=10), [])  # 22 点结束前 5 分钟以后都不开始

    def test_both_sessions(self):
        self.cfg("pet_brawl")["on"] = True
        self.assertIn((1, "pet_brawl"), self.at(12, 0))
        self.assertNotIn((1, "pet_brawl"), self.at(13, 0))
        self.assertIn((1, "pet_brawl"), self.at(20, 0))  # 中午没打完的晚上接着打
        self.assertEqual(self.cfg("pet_brawl")["done"], {"day": "2026-10-08", "windows": [0, 1]})

    def test_tongtian_follows_table_open_days(self):
        with mock.patch.object(timeplan, "_open_time", return_value=[]):  # 平时的表：工作日不开
            self.assertNotIn((None, "tongtian_hire"), self.at(19, 30))
        self.assertIn((None, "tongtian_hire"), self.at(19, 30))  # 国庆那版的表：每天开

    def test_escort_starts_until_last_minute(self):
        self.s.data["global"] = {"tongtian_escort": {"on": True, "session": "noon", "delay": 0, "accounts": {"u": {"on": True}}}}
        self.assertEqual([t for _s, t in self.at(14, 53)], ["tongtian_escort"])  # 还剩 7 分钟：能发一条是一条
        self.assertEqual([t for _s, t in self.at(14, 59, day=9)], [])  # 最后 1 分钟不再开始

    def test_open_until_is_end_of_current_window(self):
        from datetime import datetime, timedelta, timezone
        cn = timezone(timedelta(hours=8))
        at = lambda h, m: datetime(2026, 10, 1, h, m, tzinfo=cn).timestamp()
        self.assertEqual(timeplan.open_until("tongtian_escort", at(14, 53)), at(15, 0))
        self.assertIsNone(timeplan.open_until("tongtian_escort", at(15, 0)))
        self.assertIsNone(timeplan.open_until("tongtian_escort", None))

    def test_world_boss_weekend_split_at_15(self):
        """周末一整段 9–21 点：中午 9–15、晚上 15–21；“都做”= 中午做了晚上不做，中午没做才晚上做。"""
        W = timeplan.WORLD
        sat = lambda h, m: datetime(2026, 11, 7, h, m)  # 周六
        with mock.patch.object(timeplan, "windows", return_value=[timeplan.Window(540, 1260)]):
            self.assertEqual(timeplan.sessions(W, 5), ([timeplan.Window(540, 900), timeplan.Window(900, 1260)], True))
            cfg = lambda session: {"on": True, "session": session, "delay": 5, "weekdays": [5]}
            noon, evening, both = cfg("noon"), cfg("evening"), cfg("both")
            self.assertFalse(timeplan.due(W, evening, sat(9, 30)))
            self.assertFalse(timeplan.due(W, evening, sat(15, 4)))
            self.assertTrue(timeplan.due(W, evening, sat(15, 5)))
            self.assertTrue(timeplan.due(W, noon, sat(9, 5)))
            self.assertFalse(timeplan.due(W, noon, sat(15, 5)))
            self.assertTrue(timeplan.due(W, both, sat(9, 5)))
            self.assertFalse(timeplan.due(W, both, sat(15, 5)))  # 中午场做过了
            late = cfg("both")
            self.assertTrue(timeplan.due(W, late, sat(16, 0)))  # 中午关机没做：晚上做
            self.assertFalse(timeplan.due(W, late, sat(17, 0)))
        self.assertEqual(timeplan.sessions("pvp_match", 0), (timeplan.windows("pvp_match", 0), False))  # 别的任务不劈

    def test_world_boss_needs_weekdays(self):
        self.s.data["global"] = {"world_boss": {"on": True, "session": "noon", "delay": 0, "weekdays": [], "accounts": {"u": {"on": True}}}}
        self.assertFalse(globalcfg.enabled(self.s))
        self.assertEqual(self.at(12, 30, day=12), [])
        self.cfg("world_boss")["weekdays"] = [0, 5]
        self.assertEqual(self.at(12, 30, day=12), [(None, "world_boss")])  # 周一 12–15
        self.assertEqual(self.at(12, 30, day=13), [])  # 周二没选
        self.assertEqual(self.at(9, 30, day=17), [(None, "world_boss")])  # 周六 9–21 点

    def test_apply_any_time_in_period_once_per_week(self):
        self.s.data["global"] = {"arena_apply": {"on": True, "accounts": {"u": {"slots": [1]}}}}
        self.assertEqual(self.at(15, 59, day=9), [])  # 周五 16 点前
        self.assertEqual(self.at(20, 0, day=10), [(1, "arena_apply")])  # 周五关机：周六开机照样报
        self.assertEqual(self.at(10, 0, day=11), [])  # 这一届报过了
        self.assertEqual(self.at(22, 0, day=11), [])  # 周日 22 点报名结束
        self.assertEqual(self.at(16, 0, day=16), [(1, "arena_apply")])  # 下一届

    def test_retry_after_network_failure_per_save(self):
        """网络失败按“登录名#存档”各约各的：存档 1 失败只重试存档 1。"""
        self.cfg("tongtian_hire")["on"] = False
        cfg = self.cfg("pvp_match")
        retries = lambda h, m: globalcfg.retries_due(self.s, datetime(2026, 10, 8, h, m))
        self.assertEqual(self.at(19, 5), [(1, "pvp_match"), (3, "pvp_match")])
        key = globalcfg.retry_key("u", 1)
        self.assertEqual(globalcfg.split_key(key), ("u", 1))
        self.assertEqual(timeplan.schedule_retry("pvp_match", cfg, key, datetime(2026, 10, 8, 19, 6)), "19:11")
        self.assertEqual(retries(19, 10), [])
        self.assertEqual(retries(19, 11), [("pvp_match", key)])
        self.assertEqual(retries(19, 12), [])
        self.assertEqual(self.at(19, 12), [])  # 时段本身已经做过
        self.assertEqual(timeplan.schedule_retry("pvp_match", cfg, key, datetime(2026, 10, 8, 19, 12)), "19:27")
        self.assertEqual(timeplan.schedule_retry("pvp_match", cfg, key, datetime(2026, 10, 8, 19, 28)), "19:58")
        self.assertIsNone(timeplan.schedule_retry("pvp_match", cfg, key, datetime(2026, 10, 8, 20, 0)))  # 三次用完
        self.assertNotIn(key, cfg["retry"])
        self.assertIsNone(timeplan.schedule_retry("pvp_match", cfg, key, datetime(2026, 10, 8, 20, 56)))  # 时段里来不及了
        self.assertEqual(globalcfg.split_key(globalcfg.retry_key("u#x", None)), ("u#x", None))  # 账号级没有存档

    def test_network_failure_text(self):
        self.assertTrue(timeplan.network_failure("登录/进入角色失败：连接游戏服务器失败（网络）：timed out"))
        self.assertTrue(timeplan.network_failure("登录/进入角色失败：连接 4399 平台失败（网络/证书）：x"))
        self.assertFalse(timeplan.network_failure("登录/进入角色失败：账号或密码有误：密码错误"))


class RealDpapiTests(unittest.TestCase):
    def test_roundtrip(self):
        sealed = store.seal("真实密码")
        self.assertNotIn("真实密码", sealed)
        self.assertEqual(store.unseal(sealed), "真实密码")

    def test_reset_save_clears_only_that_slots_settings_and_read_profile(self):
        acc = {"saves": {"1": {"tasks": ["a"], "options": {"a": {"times": 3}}, "no_pickup": ["x"], "resume": ["a"], "profile": {"lv": 9}},
                         "2": {"tasks": ["b"]}}}
        store.Store.reset_save(acc, 1)
        store.Store.reset_save(acc, 5)  # 没有的存档：不创建
        self.assertEqual(acc["saves"], {"1": {}, "2": {"tasks": ["b"]}})  # 读取到的角色信息（羽毛、时光之舟等）也清掉


if __name__ == "__main__":
    unittest.main()

