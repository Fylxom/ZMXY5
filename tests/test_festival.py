"""秋枫盛典每日流程、在线等待和奖励领取的离线测试。"""

import unittest
import threading
from unittest import mock

from zmxy import data, dungeon, login, protocol as p, quest, state
from zmxy.core import Session, TaskContext, UncertainOutcome, load_tasks
from zmxy.tasks import daily_quest
from zmxy.tasks.events import festival
from tests.support import Fake, run, ts


NOW = ts(2026, 10, 13)
FESTIVAL_IDS = (9554003, 9554004, 9554005)


def festival_info(statuses):
    return {
        "p1": {"level": 1},
        "quest": [{"id": ident, "status": statuses[ident]} for ident in FESTIVAL_IDS],
        "holidayWelfare": {"holidayMoney": 0, "open": {}, "rewardHistory": {}},
    }


try:
    data.path_of("HolidayWelfareHelper")
except data.DataError:
    HAS_FESTIVAL_TABLES = False
else:
    HAS_FESTIVAL_TABLES = True


class FestivalWindowTests(unittest.TestCase):
    def test_window_uses_helper_activity_without_requiring_weekly_snapshot(self):
        # 窗口路由单测使用独立配置，不依赖9月旧快照是否包含10月活动。
        for is_open in (True, False):
            with mock.patch.object(state, "row", return_value={"activityId": 2057}) as row, \
                 mock.patch.object(state, "activity_open", return_value=is_open) as window:
                self.assertEqual(festival.is_open(NOW), is_open)
                row.assert_called_once_with("HolidayWelfareHelper", 1)
                window.assert_called_once_with(2057, NOW)

    def test_window_without_activity_id_is_closed(self):
        with mock.patch.object(state, "row", return_value={}), \
             mock.patch.object(state, "activity_open") as window:
            self.assertFalse(festival.is_open(NOW))
            window.assert_not_called()


@unittest.skipUnless(HAS_FESTIVAL_TABLES, "测试快照尚未包含秋枫盛典表")
class FestivalTests(unittest.TestCase):
    def test_dungeon_runner_uses_daily_main_elite_priority(self):
        session = Session(p.ClientConfig(uid=1, username="u"))
        session.bind(load_tasks()[festival.ID])
        cases = (
            (("festival",), None),
            (("festival", "dungeon_elite"), "dungeon_elite"),
            (("festival", "dungeon_main", "dungeon_elite"), "dungeon_main"),
            (("festival", "daily_quest", "dungeon_main", "dungeon_elite"), "daily_quest"),
        )
        for tasks, expected in cases:
            with self.subTest(tasks=tasks):
                self.assertEqual(festival.dungeon_runner(TaskContext(session, tasks=tasks)), expected)

    def test_main_multiple_levels_inject_festival_stats_once(self):
        current = dict.fromkeys(FESTIVAL_IDS, quest.DOING)
        info = {**festival_info(current), "dungeon": {}}
        conn = Fake(lambda route, body: {"rnd": 7, "drops": []} if route == dungeon.R_CREATE else None)

        with mock.patch("zmxy.quota.locked", return_value=None):
            result = run(
                "dungeon_main",
                conn,
                info,
                options={"levels": [1, 2], "times": 1},
                now=NOW,
                tasks=("festival", "dungeon_main"),
                all_options={"festival": {}},
            )

        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(len(conn.bodies("finishSingleCopy")), 2)
        first, second = conn.bodies("finishSingleCopy")
        self.assertEqual({name: first[name] for name in dungeon.BATTLE_STAT_FIELDS},
                         {"maxCombo": 15, "killMonsterCnt": 36, "useMagicWeaponCnt": 3})
        self.assertEqual({name: second[name] for name in dungeon.BATTLE_STAT_FIELDS},
                         {"maxCombo": 0, "killMonsterCnt": 0, "useMagicWeaponCnt": 0})

    def test_only_daily_challenge_page_is_used(self):
        helper = {
            "activityId": 2057,
            "initTask_1": "9554001;9554002",
            "initTask_2": "9554111;9554161",
            "initTask_8": "9554711;9554761",
        }
        with mock.patch.object(festival.state, "activity_open", return_value=True):
            self.assertEqual(festival._open_task_ids(helper, NOW), [9554001, 9554002])

    def test_festival_uses_mainline_fallback_for_pending_battle_goals(self):
        current = dict.fromkeys(FESTIVAL_IDS, quest.DOING)
        conn = Fake()
        first_main = dungeon.level(1)

        with mock.patch.object(Session, "refresh", return_value=festival_info(current)), \
             mock.patch.object(festival.dungeon, "level", return_value=first_main), \
             mock.patch.object(festival.dungeon, "run_level", return_value=(1, None)) as run_level:
            result = run("festival", conn, festival_info(current), now=NOW, tasks=("festival",))

        self.assertEqual(result.status, "submitted", result.detail)
        self.assertTrue(result.todo)
        self.assertEqual(conn.sent, [])
        run_level.assert_called_once()
        self.assertIs(run_level.call_args.args[1], first_main)
        self.assertEqual(run_level.call_args.kwargs["battle_stats"], {
            "maxCombo": 15, "killMonsterCnt": 36, "useMagicWeaponCnt": 3,
        })
        self.assertFalse(run_level.call_args.kwargs["collect"])
        self.assertFalse(run_level.call_args.kwargs["win"])

    def test_only_item_goal_left_still_uses_mainline_fallback(self):
        current = dict.fromkeys(FESTIVAL_IDS, quest.DOING)
        first_main = dungeon.level(1)

        with mock.patch.object(Session, "refresh", return_value=festival_info(current)),              mock.patch.object(festival, "_battle_stats_for", return_value={}),              mock.patch.object(festival, "battle_item_plan", side_effect=[([101009001], None), ([], None)] + [([], None)] * 9),              mock.patch.object(festival.dungeon, "level", return_value=first_main),              mock.patch.object(festival.dungeon, "run_level", return_value=(1, None)) as run_level:
            result = run("festival", Fake(), festival_info(current), now=NOW, tasks=("festival",))

        self.assertEqual(result.status, "submitted", result.detail)
        self.assertNotIn("关卡道具目标", result.detail)
        run_level.assert_called_once()
        self.assertEqual(run_level.call_args.kwargs["battle_items"], [101009001])
        self.assertFalse(run_level.call_args.kwargs["win"])

    def test_daily_run_keeps_real_stats_and_uses_configured_item(self):
        current = dict.fromkeys(FESTIVAL_IDS, quest.DOING)
        info = {
            "vip": {"level": 0},
            "dungeon": {"30600": {"id": 30600, "dailyCount": 0}},
            **festival_info(current),
        }

        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": [{"monsterId": 1, "index": 0, "subType": 123}]}
            return None

        conn = Fake(handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": NOW})
        session.bind(load_tasks()["daily_quest"])
        ctx = TaskContext(session, tasks=("daily_quest", "festival"))
        with mock.patch.object(festival, "battle_item_plan", return_value=([101009001], None)):
            runner = daily_quest._Run(ctx, [30600], None, {30600: 1}, "")

        with mock.patch("zmxy.quota.locked", return_value=None):
            done, stop = runner.attempt(30600)

        self.assertEqual((done, stop), (1, None))
        self.assertIn("syncDropInfo", conn.routes())
        self.assertEqual(conn.routes()[0:4], ["createSinglePlayerCopy", "useItem", "syncDropInfo", "finishSingleCopy"])
        self.assertEqual({key: conn.bodies("useItem")[0][key] for key in ("index", "count", "id")},
                         {"index": 0, "count": 1, "id": "101009001"})
        finish = conn.bodies("finishSingleCopy")[0]
        self.assertEqual({name: finish[name] for name in dungeon.BATTLE_STAT_FIELDS},
                         {"maxCombo": 15, "killMonsterCnt": 36, "useMagicWeaponCnt": 3})

    def test_daily_multiple_levels_inject_festival_payload_once(self):
        current = dict.fromkeys(FESTIVAL_IDS, quest.DOING)
        info = {
            "vip": {"level": 0},
            "dungeon": {
                "30600": {"id": 30600, "dailyCount": 0},
                "30610": {"id": 30610, "dailyCount": 0},
            },
            **festival_info(current),
        }

        def handler(route, body):
            if route == dungeon.R_CREATE:
                return {"rnd": 7, "drops": []}
            return None

        conn = Fake(handler)
        session = Session(p.ClientConfig(uid=1, username="u"))
        session._conn, session.info = conn, info
        session._link = login.Link(conn, session.config, [{"index": 0}], {"time": NOW})
        session.bind(load_tasks()["daily_quest"])
        ctx = TaskContext(session, tasks=("daily_quest", "festival"))
        with mock.patch.object(festival, "battle_item_plan", return_value=([101009001], None)):
            runner = daily_quest._Run(ctx, [30600, 30610], None, {30600: 1, 30610: 1}, "")

        with mock.patch("zmxy.quota.locked", return_value=None):
            self.assertEqual(runner.attempt(30600), (1, None))
            self.assertEqual(runner.attempt(30610), (1, None))

        self.assertEqual(len(conn.bodies("useItem")), 1)
        finishes = conn.bodies("finishSingleCopy")
        self.assertEqual(len(finishes), 2)
        self.assertEqual({name: finishes[0][name] for name in dungeon.BATTLE_STAT_FIELDS},
                         {"maxCombo": 15, "killMonsterCnt": 36, "useMagicWeaponCnt": 3})
        self.assertEqual({name: finishes[1][name] for name in dungeon.BATTLE_STAT_FIELDS},
                         {"maxCombo": 0, "killMonsterCnt": 0, "useMagicWeaponCnt": 0})

    def test_festival_exposes_selection_button_and_requested_items(self):
        choices = load_tasks()[festival.ID].choices
        self.assertIsNotNone(choices)
        self.assertEqual(choices.button, "选择")
        self.assertEqual(dict(choices.toggles), {
            "do_items": "使用关卡道具",
            "do_compose": "做炼丹炉",
            "do_online": "做在线任务",
        })
        self.assertEqual(dict(choices.toggle_defaults), {
            "do_items": True,
            "do_compose": True,
            "do_online": True,
        })
        self.assertEqual(
            dict(choices.items()),
            {
                101009001: "一品回元露",
                101010003: "一阶烈火咒",
                101010001: "一阶寒冰咒",
            },
        )
        self.assertEqual(choices.picks[0].key, "compose")
        self.assertEqual(dict(choices.picks[0].items({})), {
            "return_1": "一品回元露",
            "return_2": "二品回元露",
            "return_3": "三品回元露",
            "gem_all": "全部宝石",
        })
        gem = next((row for row in state.table_rows("Compose") if state.num(row.get("label")) == 46), None)
        if gem is not None:
            self.assertEqual(festival._compose_category(gem), "gem_all")

    def test_daily_non_battle_targets_use_client_routes(self):
        helper = state.row("HolidayWelfareHelper", festival.HELPER_ID)
        page_ids = festival._open_task_ids(helper, NOW) if helper else []
        rows = {ident: state.row("HolidayWelfareTask", ident) for ident in page_ids}
        rows = {ident: row for ident, row in rows.items() if row is not None}
        action_rows = {
            state.num(row.get("taskType")): (ident, row)
            for ident, row in rows.items()
            if state.num(row.get("taskType")) in (32, 28)
        }
        props = {}
        for row in state.table_rows("Prop"):
            prop_type, ident = state.num(row.get("propType")), state.num(row.get("id"))
            if prop_type in (1009, 1010, 1050) and state.num(row.get("isBattle")) == 1 and ident is not None:
                props.setdefault(prop_type, ident)
        if set(action_rows) != {28, 32} or set(props) != {1009, 1010, 1050}:
            self.skipTest("当前测试表缺少每日非战斗目标或关卡消耗品")
        item_row = state.row("Compose", 1)
        if not item_row or item_row.get("consume_type") != "item":
            self.skipTest("当前测试表缺少一品回元露普通配方")

        current = {ident: quest.DONE for ident in rows}
        current[action_rows[28][0]] = quest.DOING
        current[action_rows[32][0]] = quest.DOING
        use_count = [0]
        configured_types = {1009, 1010}
        needed_uses = sum(
            state.num(goal) or 0
            for target, goal in (festival._targets(action_rows[32][1]) or [])
            if target in configured_types
        )

        def info():
            records = []
            for ident, row in rows.items():
                progress = {
                    str(target): (goal if current[ident] in (quest.READY, quest.DONE) else 0)
                    for target, goal in (festival._targets(row) or [])
                }
                records.append({"id": ident, "status": current[ident], "progress": progress})
            bag_items = [{"id": ident, "count": 2} for ident in props.values()]
            bag_items.append({"id": 101002000, "count": 2})
            return {
                "p1": {"level": 1}, "quest": records, "bag": {"item": bag_items},
                "compose": {"level": 1}, "dungeon": {},
                "holidayWelfare": {"holidayMoney": 0, "open": {}, "rewardHistory": {}},
            }

        def handler(route, body):
            if route == festival.R_USE_ITEM:
                use_count[0] += 1
                if use_count[0] == needed_uses:
                    current[action_rows[32][0]] = quest.READY
            elif route == festival.R_COMPOSE:
                current[action_rows[28][0]] = quest.READY
            elif route == quest.R_COMMIT:
                current[body["qId"]] = quest.DONE
            return None

        conn = Fake(handler)
        def refresh(session):
            session.info = info()
            return session.info

        with mock.patch.object(Session, "refresh", refresh):
            result = run(
                "festival",
                conn,
                info(),
                options={"items": list(props.values()), "compose": ["return_1"]},
                now=NOW,
                tasks=("festival",),
            )

        self.assertEqual(result.status, "submitted", result.detail)
        start = conn.routes().index("createSinglePlayerCopy")  # 合成在前，之后只剩关卡道具目标，走主线兜底
        self.assertEqual(conn.routes()[start:start + needed_uses + 2],
                         ["createSinglePlayerCopy", *(["useItem"] * needed_uses), "finishSingleCopy"])
        self.assertEqual(len(conn.bodies("useItem")), needed_uses)
        allowed_ids = {str(props[1009]), str(props[1010])}
        self.assertTrue(all(body["id"] in allowed_ids for body in conn.bodies("useItem")))
        self.assertNotIn(str(props[1050]), [body["id"] for body in conn.bodies("useItem")])
        self.assertEqual(conn.bodies("compose"), [{"composeId": 1}])
        self.assertEqual(len(conn.bodies("commitQuest")), 2)

    def test_disabled_festival_switches_skip_their_routes(self):
        session = Session(p.ClientConfig(uid=1, username="u"))
        session.bind(load_tasks()[festival.ID])
        ctx = TaskContext(
            session,
            options={"do_items": False, "do_compose": False, "do_online": False},
            tasks=(festival.ID,),
        )
        gains = dungeon.Gains()
        rows = [
            (1, {"id": 1, "taskType": 27, "targetId": "1", "targetNum": "10"}),
            (2, {"id": 2, "taskType": 28, "targetId": "1", "targetNum": "1"}),
        ]
        with mock.patch.object(festival, "_wait_online") as online, \
             mock.patch.object(festival, "_compose_once") as compose:
            attempted, issues = festival._daily_actions(ctx, rows, {1: quest.DOING, 2: quest.DOING}, {1: {}, 2: {}}, gains)
        online.assert_not_called()
        compose.assert_not_called()
        self.assertFalse(attempted)
        self.assertEqual(issues, [])
        self.assertEqual(festival.battle_item_plan(ctx), ([], None))

    def test_selected_daily_task_never_triggers_festival_main_fallback(self):
        info = festival_info(dict.fromkeys(FESTIVAL_IDS, quest.DOING))
        conn = Fake()

        with mock.patch.object(Session, "refresh", return_value=info):
            result = run("festival", conn, info, now=NOW, tasks=("daily_quest", "festival"))

        self.assertEqual(result.status, "skipped")
        self.assertTrue(result.todo)
        self.assertEqual(conn.sent, [])


class FestivalRewardClaimTests(unittest.TestCase):
    """独立于测试表快照，验证周页开放边界和会员档位领奖契约。"""

    def test_body_only_composes_and_stage_claims_after_online(self):
        """本体按正常顺序只做炼丹炉、不领取；在线阶段先等在线，再领全部 READY（含在线目标）。"""
        ids = (9554001, 9554002, 9554003, 9554004, 9554005, 9554006)
        kinds = (27, 32, 33, 40, 34, 28)
        names = ("在线任务", "关卡道具", "最高连击", "击败怪物", "使用法宝", "炼丹炉")
        rows = {
            ident: {"id": ident, "nextTaskId": -1, "taskType": kind, "targetId": "0",
                    "targetNum": "900" if kind == 27 else "1", "name": name}
            for ident, kind, name in zip(ids, kinds, names)
        }
        statuses = {
            ids[0]: quest.DOING,
            ids[1]: quest.READY,
            ids[2]: quest.READY,
            ids[3]: quest.READY,
            ids[4]: quest.READY,
            ids[5]: quest.DOING,
        }
        events = []
        helper = {"activityId": 2057}

        def info():
            return {"quest": [{"id": ident, "status": status} for ident, status in statuses.items()],
                    "holidayWelfare": {"holidayMoney": 0, "open": {}, "rewardHistory": {}}}

        def handler(route, body):
            if route == quest.R_COMMIT:
                ident = state.num(body.get("qId"))
                statuses[ident] = quest.DONE
                events.append(f"claim:{ident}")
            return None

        def refresh(session):
            session.info = info()
            return session.info

        def compose(_ctx, row, _record, _gains):
            events.append("compose")
            statuses[state.num(row["id"])] = quest.READY
            return True, None

        def online(_ctx, *_args):
            events.append("online")
            self.assertFalse(any(e.startswith("claim") for e in events))
            return True, None

        def table_row(table, ident):
            if table == "HolidayWelfareHelper":
                return helper
            if table == "HolidayWelfareTask":
                return rows.get(state.num(ident))
            return None

        conn = Fake(handler)
        with mock.patch.object(festival.state, "row", side_effect=table_row),              mock.patch.object(festival, "_open_task_pages", return_value=[(1, list(ids))]),              mock.patch.object(festival, "battle_item_plan", return_value=([], None)),              mock.patch.object(festival, "dungeon_runner", return_value="dungeon_hidden"),              mock.patch.object(festival, "_compose_once", side_effect=compose),              mock.patch.object(festival, "_wait_online", side_effect=online),              mock.patch.object(festival, "_level_reward_ids", return_value=([], None)),              mock.patch.object(Session, "refresh", refresh):
            body = run("festival", conn, info(), now=NOW, tasks=("festival", "dungeon_hidden"))
            self.assertEqual(events, ["compose"])
            self.assertEqual(body.status, "submitted", body.detail)
            self.assertEqual(conn.bodies(quest.R_COMMIT), [])
            staged = run("festival", conn, info(), now=NOW, tasks=("festival", "dungeon_hidden"), stage=True)

        self.assertEqual(events[1], "online")
        self.assertEqual(events[2], f"claim:{ids[0]}")
        self.assertEqual(sorted(events[2:]), sorted(f"claim:{ident}" for ident in ids))
        self.assertEqual(staged.status, "submitted", staged.detail)
        self.assertEqual(staged.todo, "")

    def _run_claims(self, opened):
        helper = {
            "activityId": 2057,
            "activityTimeId": "2058;2059;2060",
            "initTask_1": "1",
            "initTask_2": "2",
            "initTask_3": "3",
            "weekNum": 2,
            "oneHolidayMoney": 100,
            "levelMax": 2,
        }
        task_rows = {
            ident: {"id": ident, "nextTaskId": -1, "taskType": 7, "targetId": "0", "targetNum": "1", "name": "挑战"}
            for ident in (1, 2, 3)
        }
        reward_rows = {
            ident: {"id": ident, "levelType": track, "subType": 101000000 + ident}
            for track in (1, 2)
            for ident in (track * 1000 + 1, track * 1000 + 2)
        }
        task_status = {1: quest.READY, 2: quest.READY, 3: quest.READY}
        history = {}

        def info():
            return {
                "quest": [{"id": ident, "status": status} for ident, status in task_status.items()],
                "holidayWelfare": {"holidayMoney": 100, "open": dict(opened), "rewardHistory": dict(history)},
            }

        def row(table, ident):
            ident = int(ident)
            if table == "HolidayWelfareHelper":
                return helper if ident == 1 else None
            if table == "HolidayWelfareTask":
                return task_rows.get(ident)
            if table == "HolidayWelfare":
                return reward_rows.get(ident)
            return None

        def handler(route, body):
            if route == quest.R_COMMIT:
                task_status[body["qId"]] = quest.DONE
            elif route == festival.R_LEVEL_CLAIM:
                history[str(body["id"])] = 1
            return None

        conn = Fake(handler)

        def refresh(session):
            session.info = info()
            return session.info

        is_open = lambda activity_id, _now: int(activity_id) in {2057, 2058, 2059}
        with mock.patch.object(festival.state, "row", side_effect=row), \
             mock.patch.object(festival.state, "activity_open", side_effect=is_open), \
             mock.patch.object(Session, "refresh", refresh):
            result = run("festival", conn, info(), now=NOW, tasks=("festival",), stage=True)
        return result, conn

    def test_claims_completed_tasks_only_from_open_week_pages_and_free_level_track(self):
        result, conn = self._run_claims({"1": 1})

        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual([body["qId"] for body in conn.bodies(quest.R_COMMIT)], [1, 2])
        self.assertEqual([body["id"] for body in conn.bodies(festival.R_LEVEL_CLAIM)], [1001, 1002])
        self.assertIn("等级奖励 2 项", result.detail)

    def test_activated_membership_claims_free_and_paid_level_tracks(self):
        result, conn = self._run_claims({"1": 1, "2": 1})

        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(
            [body["id"] for body in conn.bodies(festival.R_LEVEL_CLAIM)],
            [1001, 2001, 1002, 2002],
        )


class FestivalOnlineClaimTests(unittest.TestCase):
    """存档在线进度不随当前会话增长时，等待与单项领取之间不得重新进角。"""

    def _run_online(self, *, progress=142, online=True, waited=True, interrupt=False,
                    refusal=None, keep_pending=False, status=quest.DOING,
                    clock="normal", reconnect=False, now=NOW):
        task_status = status
        elapsed = 0
        connected = True
        events = []
        stop = threading.Event()
        task_row = {"id": 1, "nextTaskId": -1, "taskType": 27, "targetId": "0", "targetNum": "900", "name": "在线"}

        def info():
            return {"quest": [{"id": 1, "status": task_status, "progress": {"0": progress}}],
                    "holidayWelfare": {"holidayMoney": 0, "open": {}, "rewardHistory": {}}}

        def row(table, ident):
            if table == "HolidayWelfareHelper":
                return {"activityId": 2057}
            return task_row if table == "HolidayWelfareTask" and int(ident) == 1 else None

        def handler(route, _body):
            nonlocal task_status
            if route == festival.R_SERVER_TIME:
                events.append("clock")
                if clock == "missing":
                    return {}
                offset = elapsed if clock == "normal" else -elapsed if clock == "backwards" else 0
                return {"serverTime": (now + offset) * 1000}
            if route == quest.R_COMMIT:
                events.append("claim")
                if isinstance(refusal, BaseException):
                    raise refusal
                if refusal:
                    return refusal
                if not keep_pending:
                    task_status = quest.DONE
            return None

        def refresh(session):
            events.append("refresh")
            session.info = info()
            return session.info

        def sleep(_ctx, seconds, background=True):
            nonlocal elapsed, connected
            self.assertFalse(background)
            events.append(("wait", seconds))
            elapsed += seconds
            connected = waited
            if reconnect:
                _ctx.session._conn = Fake(handler)
            if interrupt:
                stop.set()
            return waited and not stop.is_set()

        self.conn = conn = Fake(handler)
        with mock.patch.object(festival.state, "row", side_effect=row), \
             mock.patch.object(festival, "_open_task_pages", return_value=[(1, [1])]), \
             mock.patch.object(festival, "battle_item_plan", return_value=([], None)), \
             mock.patch.object(festival, "_level_reward_ids", return_value=([], None)), \
             mock.patch.object(Session, "refresh", refresh), \
             mock.patch.object(Session, "live", new_callable=mock.PropertyMock, side_effect=lambda: connected), \
             mock.patch.object(TaskContext, "sleep", sleep):
            result = run("festival", conn, info(), options={"do_online": online}, now=now, stop=stop, tasks=("festival",), stage=True)
        return result, conn, events

    def test_waits_for_142_of_900_then_claims_before_refresh_even_while_doing(self):
        result, conn, events = self._run_online()
        self.assertEqual(sum(e[1] for e in events if isinstance(e, tuple)), 759)
        self.assertEqual(events.count("clock"), 4)
        self.assertEqual(events[-2:], ["claim", "refresh"])
        self.assertEqual(events.count("refresh"), 2)
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 1}])
        self.assertEqual((result.status, result.todo), ("submitted", ""), result.detail)

    def test_server_progress_at_goal_is_claimed_without_waiting(self):
        result, conn, events = self._run_online(progress=900)
        self.assertEqual(events, ["refresh", "claim", "refresh"])
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 1}])
        self.assertEqual((result.status, result.todo), ("submitted", ""), result.detail)

    def test_missing_server_time_does_not_wait_or_claim(self):
        result, conn, events = self._run_online(clock="missing")
        self.assertEqual(events, ["refresh", "clock"])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertIn("时间同步失败", result.detail)

    def test_stalled_or_backwards_server_time_does_not_claim(self):
        for clock in ("stalled", "backwards"):
            with self.subTest(clock=clock):
                result, conn, events = self._run_online(clock=clock)
                self.assertEqual(conn.bodies("commitQuest"), [])
                self.assertEqual(sum(e[1] for e in events if isinstance(e, tuple)), 300)
                self.assertIn("时间未正常推进", result.detail)

    def test_replaced_connection_does_not_claim(self):
        result, conn, _ = self._run_online(reconnect=True)
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertIn("Pomelo 连接中断", result.detail)

    def test_server_midnight_does_not_claim_previous_daily_task(self):
        midnight = (NOW + 28800) // 86400 * 86400 - 28800
        result, conn, _ = self._run_online(now=midnight + 86300)
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertIn("每日重置", result.detail)

    def test_disabled_online_task_does_not_wait_or_claim(self):
        result, conn, events = self._run_online(online=False)
        self.assertEqual(events, ["refresh"])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertIn("142/900", result.todo)

    def test_malformed_online_progress_does_not_wait_or_claim(self):
        result, conn, events = self._run_online(progress=[])
        self.assertEqual(events, ["refresh"])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("服务器未返回在线任务进度", result.detail)

    def test_stopped_wait_does_not_claim(self):
        result, conn, _ = self._run_online(interrupt=True)
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("用户停止", result.detail)

    def test_stop_before_any_action_is_skipped(self):
        stop = threading.Event()
        stop.set()
        row = lambda table, ident: {"activityId": 2057} if table == "HolidayWelfareHelper" else None
        info = {"quest": [], "holidayWelfare": {"holidayMoney": 0, "open": {}, "rewardHistory": {}}}
        conn = Fake()
        with mock.patch.object(festival.state, "row", side_effect=row),              mock.patch.object(festival, "_open_task_pages", return_value=[(1, [1])]),              mock.patch.object(Session, "refresh", return_value=info):
            result = run("festival", conn, info, now=NOW, stop=stop, tasks=("festival",))
        self.assertEqual(result.status, "skipped", result.detail)
        self.assertEqual(conn.sent, [])

    def test_disconnected_wait_does_not_claim(self):
        result, conn, _ = self._run_online(waited=False)
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("Pomelo 连接中断", result.detail)

    def test_refused_online_claim_is_not_retried(self):
        result, conn, _ = self._run_online(refusal="在线时间不足")
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 1}])

    def test_unknown_online_claim_is_not_retried(self):
        with self.assertRaises(UncertainOutcome):
            self._run_online(refusal=TimeoutError("领奖超时"))
        self.assertEqual(self.conn.bodies("commitQuest"), [{"qId": 1}])

    def test_unconfirmed_online_claim_is_reported_without_repeating(self):
        result, conn, _ = self._run_online(keep_pending=True)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertIn("未确认已领取", result.detail)
        self.assertEqual(conn.bodies("commitQuest"), [{"qId": 1}])

    def test_claimed_online_task_is_not_replayed(self):
        result, conn, events = self._run_online(status=quest.DONE)
        self.assertEqual(events, ["refresh"])
        self.assertEqual(conn.bodies("commitQuest"), [])
        self.assertEqual(result.todo, "")

