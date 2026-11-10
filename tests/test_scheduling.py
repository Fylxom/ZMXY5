"""后台每日定时：离线执行、互斥、登记定义和无交互失败处理，不登记真实系统任务。"""

import os
import queue
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

from zmxy import core, entry, scheduling, store, timeplan, timerlog

try:
    from PySide6.QtWidgets import QApplication
    from zmxy import gui
    from zmxy.dialogs import ChoiceDialog
    from zmxy.instance import Instance
    from zmxy.mainwin import events, rules
    from zmxy.tasks import boss_raid, chapter_role, last_rewards
    from zmxy.widgets import Card
except ImportError:
    QApplication = None


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(Path(self.tmp.name) / "settings.json")
        self.store.accounts[:] = [{"username": "u", "saves": {"2": {}}}]

    def test_enabled_uses_timer_switch_without_requiring_task_selection(self):
        cfg = self.store.data["global"] = {}
        self.assertFalse(scheduling.enabled(self.store))
        cfg["world_boss"] = {"on": True, "session": "noon", "accounts": {"u": {"on": True}}, "done": {"day": "2026-10-12", "windows": [0]}}
        self.assertFalse(scheduling.enabled(self.store))  # 世界Boss没选星期
        cfg["world_boss"]["weekdays"] = [0]
        self.assertTrue(scheduling.enabled(self.store))
        cfg["world_boss"]["session"] = "midnight"
        self.assertFalse(scheduling.enabled(self.store))
        cfg["world_boss"].update(on=False, session="noon")
        self.assertFalse(scheduling.enabled(self.store))
        cfg["arena_apply"] = {"on": True, "accounts": {"u": {"slots": [2]}}}  # 报名类只要开关和存档
        self.assertTrue(scheduling.enabled(self.store))

    def test_targets_use_existing_login_accounts_only(self):
        self.store.data["global"] = {"tongtian_hire": {"on": True, "session": "noon", "accounts": {"gone": {"on": True}}}}
        self.assertFalse(scheduling.enabled(self.store))
        self.store.data["global"]["tongtian_hire"]["accounts"]["u"] = {"on": True}
        self.assertTrue(scheduling.enabled(self.store))

    def test_removed_tasks_and_malformed_containers_do_not_enable_background(self):
        self.store.data["global"] = {"removed_task": {"on": True, "session": "noon", "accounts": {"u": {"slots": [2]}}},
                                     "garden": {"on": True, "at": "20:30", "accounts": {"u": {"slots": [2]}}},
                                     "pvp_match": {"on": True, "session": "both", "accounts": [1]}}
        self.assertFalse(scheduling.enabled(self.store))  # 药园不在定时里
        self.store.data["global"]["pvp_match"]["accounts"] = {"u": {"slots": [2]}}
        self.assertTrue(scheduling.enabled(self.store))
        self.store.data["global"] = [1]
        self.assertFalse(scheduling.enabled(self.store))

    def test_xml_keeps_unicode_spaces_and_shell_characters_as_literal_arguments(self):
        command = [r'C:\中文目录 & tools\ZMXY5.exe', '--scheduled-run', 'a"b & c']
        root = ET.fromstring(scheduling.task_xml(command, Path(r'C:\中文目录 & tools'), datetime(2026, 10, 12, 23, 59, 55)))
        ns = {"t": scheduling.NS}
        text = lambda path: root.find(path, ns).text
        self.assertEqual(text("t:Actions/t:Exec/t:Command"), command[0])
        self.assertIn('a\\"b & c', text("t:Actions/t:Exec/t:Arguments"))
        self.assertEqual(text("t:Triggers/t:TimeTrigger/t:StartBoundary"), "2026-10-13T00:00:00")
        self.assertEqual(text("t:Principals/t:Principal/t:LogonType"), "InteractiveToken")
        self.assertEqual(text("t:Settings/t:MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(text("t:Settings/t:ExecutionTimeLimit"), "PT0S")
        self.assertEqual(text("t:Settings/t:WakeToRun"), "false")

    def test_packaged_command_uses_outer_exe_instead_of_unpacked_child(self):
        exe = Path(self.tmp.name) / "中文 ZMXY5.exe"
        exe.touch()
        with mock.patch.dict(scheduling.__dict__, {"__compiled__": object()}), mock.patch.object(sys, "argv", [str(exe)]), \
                mock.patch.object(sys, "executable", r"C:\Temp\unpacked\child.exe"):
            command, directory = scheduling.launch_command()
        self.assertEqual(command, [str(exe.resolve()), "--scheduled-run"])
        self.assertEqual(directory, exe.parent)

    def test_all_off_removes_only_own_named_task_without_needing_launcher(self):
        with mock.patch.object(scheduling, "register") as register, mock.patch.object(scheduling, "launch_command") as launcher:
            self.assertFalse(scheduling.sync(self.store))
        launcher.assert_not_called()
        register.assert_called_once_with(scheduling.task_name(self.store.path), None)

    def test_no_due_task_exits_without_loading_gui_or_claiming_timer(self):
        fake = mock.Mock()
        with mock.patch.object(sys, "argv", ["program", scheduling.FLAG]), mock.patch.object(store, "Store") as cls, \
                mock.patch.dict(sys.modules, {"zmxy.gui": types.SimpleNamespace(main=fake)}):
            self.enterContext(mock.patch.object(globalcfg_mod(), "due", return_value=[]))
            self.enterContext(mock.patch.object(globalcfg_mod(), "retries_due", return_value=[]))
            entry.main()
        fake.assert_not_called()
        cls.return_value.save.assert_not_called()

    def test_due_preflight_passes_background_flag_without_saving_done(self):
        fake = mock.Mock()
        with mock.patch.object(sys, "argv", ["program", scheduling.FLAG]), mock.patch.object(store, "Store") as cls, \
                mock.patch.dict(sys.modules, {"zmxy.gui": types.SimpleNamespace(main=fake)}):
            self.enterContext(mock.patch.object(globalcfg_mod(), "due", return_value=["pvp_match"]))
            entry.main()
        fake.assert_called_once_with(background=True)
        cls.return_value.save.assert_not_called()


@unittest.skipIf(QApplication is None, "未安装 PySide6")
class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store.Store(Path(self.tmp.name) / "settings.json")
        self.store.accounts[:] = [{"username": "u", "name": "定时账号", "saves": {"2": {
            "tasks": [], "options": {"pvp_match": {"mode": "saved"}}}}}]
        self.store.data["global"] = {"pvp_match": {"on": True, "session": "both", "delay": 0, "accounts": {"u": {"slots": [2]}}}}
        allday = mock.patch.object(timeplan, "windows", return_value=[timeplan.Window(0, 25 * 60)])  # 全天开放：测试几点跑都在时段里
        allday.start()
        self.addCleanup(allday.stop)
        self.log = Path(self.tmp.name) / "timer_log.txt"
        patch = mock.patch.object(timerlog, "path", return_value=self.log)
        patch.start()
        self.addCleanup(patch.stop)
        with mock.patch.object(gui, "Store", return_value=self.store):
            self.win = gui.App(background=True)
        self.win._timer.stop()
        self.win._net_ok_until = float("inf")  # 网络当作已通；等联网的测试自己改回来
        self.addCleanup(self.win.close)
        self.addCleanup(lambda: self.app.setQuitOnLastWindowClosed(True))

    def drain(self):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                kind, data = self.win.events.get(timeout=0.02)
            except queue.Empty:
                if not self.win.jobs and not self.win._schedule_busy:
                    return
            else:
                getattr(self.win, f"_on_{kind}")(data)
        self.fail("后台线程未按时结束")

    def test_background_round_uses_saved_slot_and_options_logs_and_never_shows_window(self):
        accounts = []

        def execute(items, *, emit, stop):
            accounts.extend(items)
            emit(items[0].name, core.TaskResult("pvp_match", core.PROGRESS, "正在执行"))
            emit(items[0].name, core.TaskResult("pvp_match", core.SUBMITTED, "离线完成"))
            return {items[0].name: [core.TaskResult("pvp_match", core.SUBMITTED)]}

        with mock.patch.object(core, "run_batch", side_effect=execute):
            self.win._tick_timers()
            self.drain()
            self.win._tick_timers()  # 同一分钟又被 Windows 触发，也不重跑
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].config.record_index, 1)
        self.assertEqual(accounts[0].tasks, ("pvp_match",))
        self.assertEqual(accounts[0].options["pvp_match"], {"mode": "saved"})
        self.assertFalse(self.win.isVisible())
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("离线完成", text)
        self.assertIn("执行结束", text)
        self.assertEqual(globalcfg_mod().due(store.Store(self.store.path)), [])

    def test_recognizing_returning_limited_event_moves_it_up_but_leaves_switch_off(self):
        acc = self.store.accounts[0]
        self.store.save_cfg(acc, 2)["tasks"] = ["pet_elf", "cloud_ladder"]
        self.store.data["limited"] = {"at": 0, "ended": []}

        with mock.patch.object(self.win, "_build_limited"), mock.patch.object(self.win, "on_select"):
            self.win._on_limited({"at": 1, "ended": ["cloud_ladder"]})  # 结束：照旧关掉
            self.assertEqual(self.store.save_view(acc, 2)["tasks"], ["pet_elf"])
            self.assertIn("cloud_ladder", self.win._ended_ids())
            self.win._on_limited({"at": 2, "ended": []})  # 重新开放：移回上面，开关不开
        self.assertNotIn("cloud_ladder", self.win._ended_ids())
        self.assertEqual(self.store.save_view(acc, 2)["tasks"], ["pet_elf"])

    def test_first_login_after_table_update_recognizes_once(self):
        from zmxy import data, runner
        from zmxy.mainwin import limited as page
        self.store.data["limited"] = {"at": 1, "ended": [], "tables": "cg1_20000101_1_extracted"}
        self.assertTrue(self.win._tables_stale())
        self.win.events.queue.clear()
        runner._tell_clock(1_700_000_000)  # 工作线程里进角成功
        self.assertEqual(self.win.events.get_nowait(), ("clock", 1_700_000_000))
        with mock.patch.object(page, "ended_ids", return_value=["cloud_ladder"]) as judge, \
             mock.patch.object(self.win, "_build_limited"), mock.patch.object(self.win, "on_select"):
            self.win._on_clock(1_700_000_000)
            self.win._on_clock(1_700_000_100)  # 同一版表：不再识别
        judge.assert_called_once()
        saved = self.store.data["limited"]
        self.assertEqual((saved["at"], saved["ended"], saved["tables"]), (1_700_000_000, ["cloud_ladder"], data.TABLE_ROOT.name))
        self.assertFalse(self.win._tables_stale())

    def test_manual_recognition_also_records_table_version(self):
        from zmxy import data
        with mock.patch.object(self.win, "_build_limited"), mock.patch.object(self.win, "on_select"):
            self.win._on_limited({"at": 2, "ended": []})
        self.assertEqual(self.store.data["limited"]["tables"], data.TABLE_ROOT.name)

    def test_double11_dog_switch_saves_and_returns_after_recognition(self):
        tid = "double11_dog"
        self.win.current = 0
        self.win.acc_slots[self.win._acc_key(self.store.accounts[0])] = 2
        self.win.on_select()
        self.assertIn(tid, self.win.task_vars)
        self.assertIn(tid, [spec.id for spec in self.win.limited_specs])
        self.win.task_vars[tid].set(True)
        self.win.save_tasks()
        restored = store.Store(self.store.path)
        self.assertIn(tid, restored.save_view(restored.accounts[0], 2)["tasks"])
        self.win._on_limited({"at": 1, "ended": [tid]})
        self.assertFalse(self.win.task_vars[tid].get())
        self.win._on_limited({"at": 2, "ended": []})
        self.assertFalse(self.win.task_vars[tid].get())  # 返场不自动打开

    def test_save_event_end_closes_only_that_save_and_reset_leaves_switch_off(self):
        acc = self.store.accounts[0]
        key = chapter_role.END
        task_id = rules.SAVE_END[key]
        self.store.save_cfg(acc, 2).update(tasks=[task_id], profile={})
        self.store.save_cfg(acc, 3).update(tasks=[task_id], profile={})

        self.win._on_save_end((acc, 2, {key: True}))
        self.assertEqual(self.store.save_view(acc, 2)["tasks"], [])
        self.assertEqual(self.store.save_view(acc, 3)["tasks"], [task_id])

        self.win._on_save_end((acc, 2, {key: False}))  # 完成标记重置：开关不自动打开
        self.assertEqual(self.store.save_view(acc, 2)["tasks"], [])
        self.assertFalse(self.store.save_view(acc, 2)["profile"][key])

    def test_timer_page_builds_and_saves_settings(self):
        """定时页（账号栏最上面的“定时”）：所有定时项一页，改了直接存进 store.data["global"]。"""
        from zmxy import globalcfg
        self.win.show_global()
        self.assertTrue(self.win.global_view)
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["global"])
        self.assertEqual(sorted(self.win.global_clocks), sorted(globalcfg.CLOCK_ITEMS))
        self.win._save_session("pet_brawl", "noon")
        self.win._save_delay("pet_brawl", "abc")  # 写错的分钟数不覆盖
        self.win._save_delay("pet_brawl", "12")
        cfg = globalcfg.section(store.Store(self.store.path), "pet_brawl")
        self.assertEqual((cfg["session"], cfg["delay"]), ("noon", 12))
        self.win._toggle_global_day("world_boss", mock.Mock(on=True), 2)
        self.assertEqual([n for _, n in globalcfg.rows(self.win.store)], [2])  # 只列出勾了的存档（斗神勾了存档 2）
        acc = self.store.accounts[0]
        self.win.row_slot.set("存档 4")
        self.win._add_row()
        self.win.row_slot.set("存档 6")
        self.win._add_row()
        self.assertEqual([n for _, n in globalcfg.rows(self.win.store)], [2, 4, 6])
        self.win._toggle_cell("pet_brawl", acc, 4)
        self.win._toggle_cell("world_boss", acc, 6)  # 账号级：勾在存档 6 → 页面上的选存档也改成 6
        self.assertTrue(self.win.table_chips[("world_boss", "u", 6)].on)
        self.assertEqual(self.win._wide_cfg(acc, "world_boss")["record"], 6)
        self.win._toggle_cell("world_boss", acc, 4)  # 换到存档 4：存档 6 那格跟着取消
        self.assertFalse(self.win.table_chips[("world_boss", "u", 6)].on)
        saved = store.Store(self.store.path).data
        self.assertEqual(saved["global"]["pet_brawl"]["accounts"]["u"], {"slots": [4]})
        self.assertEqual(saved["global"]["world_boss"]["weekdays"], [2])
        self.assertIs(saved["global"]["world_boss"]["accounts"]["u"]["on"], True)
        self.assertEqual(saved["wide"]["u"]["world_boss"]["record"], 4)
        self.win._toggle_column("arena_apply")  # 整列：一个都没勾 → 全勾
        self.assertEqual(globalcfg.entry(self.win.store, "arena_apply", "u")["slots"], [2, 4, 6])
        self.win._toggle_column("arena_apply")  # 有勾 → 全取消
        self.assertEqual(globalcfg.entry(self.win.store, "arena_apply", "u")["slots"], [])
        self.win._remove_row("u", 6)
        self.assertEqual([n for _, n in globalcfg.rows(self.win.store)], [2, 4])
        self.win.current = 0
        self.win.global_view = False
        self.win.on_select()
        self.assertIsNot(self.win.stack.currentWidget(), self.win.pages["global"])  # 点账号回到原来的页面

    def test_unpicked_not_done_and_marked(self):
        """规则 1：该选的没选就不做；定时页表里勾了的那一格标橙色，鼠标停上去写着是哪几个。"""
        from zmxy import globalcfg
        acc = self.store.accounts[0]
        acc["saves"]["1"] = {"tasks": ["wish_pool", "fulushou"], "options": {}}
        self.assertEqual(self.win.missing_picks(globalcfg.DAILY, acc, 1), ["福禄寿没选关"])
        self.assertEqual(self.win.missing_picks(globalcfg.ANCIENT, acc, 1), ["上古蛮荒没选关"])
        self.assertEqual(self.win.missing_picks(globalcfg.WEEKLY, acc, 1), ["周常页一个任务都没打开"])
        acc["saves"]["1"]["weekly"] = ["star_maze", "demon_tower"]
        self.assertEqual(self.win.missing_picks(globalcfg.WEEKLY, acc, 1), ["镇妖塔没选层组"])  # 打开了却没选
        acc["saves"]["1"]["weekly"] = ["star_maze"]
        self.assertEqual(self.win.missing_picks(globalcfg.WEEKLY, acc, 1), [])
        accounts = self.win._daily_accounts(acc, [1])
        self.assertEqual([a.tasks for a in accounts], [("wish_pool",)])
        self.assertEqual(self.win._ancient_accounts(acc, [1]), [])
        globalcfg.add_row(self.win.store, "u", 1)
        self.win.show_global()
        self.win._toggle_cell(globalcfg.DAILY, acc, 1)
        chip = self.win.table_chips[(globalcfg.DAILY, "u", 1)]
        self.assertTrue(chip.on and chip.warn)
        self.assertIn("福禄寿", chip._tip.text)
        self.assertFalse(self.win.table_chips[(globalcfg.ANCIENT, "u", 1)].warn)  # 没勾的不标
        acc["saves"]["1"]["options"]["fulushou"] = {"levels": [1]}
        self.assertEqual(self.win.missing_picks(globalcfg.DAILY, acc, 1), [])
        self.win._render_table()  # 回到定时页时重画
        self.assertFalse(self.win.table_chips[(globalcfg.DAILY, "u", 1)].warn)
        self.assertTrue(self.win._unpicked(acc, None, "world_boss"))  # 账号级：选项按登录账号存
        self.win._wide_cfg(acc, "world_boss").setdefault("options", {})["world_boss"] = {"levels": [1]}
        self.assertFalse(self.win._unpicked(acc, None, "world_boss"))

    def test_weekly_switches(self):
        """周常页每个任务一个开关（世界Boss有自己的定时，没有）；存在存档的 "weekly"，“自动配置并执行”只做打开的。"""
        self.assertIn("ten_jue", self.win.weekly_vars)
        self.assertNotIn(boss_raid.WORLD_ID, self.win.weekly_vars)
        self.win.current, self.win.global_view = 0, False
        self.win.on_select()
        self.win.weekly_vars["ten_jue"].set(True)
        self.win.save_weekly()
        self.assertEqual(self.store.save_view(self.store.accounts[0], self.win.slot)["weekly"], ["ten_jue"])
        for tid, field in self.win.count_vars.items():
            field.set("3" if tid in ("ten_jue", "star_maze") else "0")
        with mock.patch.object(self.win, "start") as start:
            self.win._run_weekly_all()
        self.assertEqual(start.call_args.kwargs["tasks"], ["ten_jue"])  # 星斗幻境有次数但没打开

    def test_world_boss_growth_plan(self):
        """世界Boss定时开着“按养成周”：执行项选项里带 plan=growth、去掉次数框的次数；关着就照次数框。"""
        from zmxy import globalcfg
        acc = self.store.accounts[0]
        self.win._wide_cfg(acc, "world_boss").setdefault("options", {})["world_boss"] = {"levels": [1], "times": 2}
        globalcfg.section(self.store, "world_boss")["growth"] = True
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._run_timed(acc, None, "world_boss")
        [account] = launch.call_args.args[0]
        self.assertEqual(account.options["world_boss"], {"levels": [1], "plan": "growth"})
        globalcfg.section(self.store, "world_boss")["growth"] = False
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._run_timed(acc, None, "world_boss")
        self.assertEqual(launch.call_args.args[0][0].options["world_boss"]["times"], 2)

    def test_timer_page_opens_after_background_registered(self):
        """后台定时已登记（真实程序启动后的常态）再点“定时”：页面要真的切过去（11-09 用户截图：标题变了、下面还是日常页）。"""
        self.win.current = 0
        self.win._on_schedule((True, ""))
        self.win.show_global()
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["global"])
        self.assertIn("后台定时已启用", self.win.schedule_status.text())
        self.win.global_view = False
        self.win.show_global()  # 再进一次（页面重建）也行
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["global"])

    def test_open_hour_tasks_are_on_weekly_page(self):
        """有开放时间的任务回到周常页“有开放时间”那一块（手动执行在这里，定时在定时页）；世界Boss在周常那一块。"""
        for tid in rules.OPEN_HOURS + (boss_raid.WORLD_ID,):
            self.assertIn(tid, self.win.exec_btns)
        for tid in ("union_boss", "garden"):
            self.assertNotIn(tid, self.win.exec_btns)
        self.assertIn("uid_lookup", self.win.summaries)  # 周常页最底下
        self.assertNotIn("timed", self.win.pages)

    def test_uid_lookup_is_last_block_of_weekly_page(self):
        layout = self.win.page_lays["weekly"]
        card = self.win.task_labels["uid_lookup"]
        while card is not None and not isinstance(card, Card):
            card = card.parentWidget()
        widgets = [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]
        self.assertIs(widgets[-1], card)

    def test_network_failure_schedules_retry(self):
        from zmxy import globalcfg
        self._at(12, 0)  # 固定时间：23:55 以后跑，重试会跨天、按规则不约
        with mock.patch.object(self.win, "_launch"):
            self.win._tick_timers()
        [name] = self.win.timed_items
        self.win.timed_names.add(name)
        result = core.TaskResult("login", core.ERROR, "登录/进入角色失败：连接游戏服务器失败（网络）：timed out")
        self.win._on_result((name, result))
        retry = globalcfg.section(self.store, "pvp_match")["retry"]
        self.assertEqual(retry[globalcfg.retry_key("u", 2)]["n"], 1)
        self.assertIn("第 1 次重试", self.log.read_text(encoding="utf-8"))
        wrong = core.TaskResult("login", core.ERROR, "登录/进入角色失败：账号或密码有误：密码错误")
        self.win._on_result((name, wrong))
        self.assertEqual(retry[globalcfg.retry_key("u", 2)]["n"], 1)  # 密码错不重试

    def test_world_boss_is_weekly_task_with_auto_count(self):
        tid = boss_raid.WORLD_ID
        self.assertIn(tid, self.win.weekly_ids)
        self.assertIn(tid, self.win.auto_ids)
        self.assertIn(tid, self.win.count_vars)
        self.assertIn(tid, self.win.weekly_run_ids)
        self.assertNotIn(tid, rules.BOSSES)
        self.assertNotIn(tid, self.win.mode_vars)
        self.assertNotIn(tid, self.win.wide_labels)
        card = self.win.task_labels[tid]
        while card is not None and not isinstance(card, Card):
            card = card.parentWidget()
        layout = self.win.page_lays["weekly"]
        card_index = next(i for i in range(layout.count()) if layout.itemAt(i).widget() is card)
        self.assertEqual(layout.itemAt(card_index - 1).layout().itemAt(0).widget().text(), "周常")

        acc = self.store.accounts[0]
        cfg = self.win._wide_cfg(acc, tid)
        boss, name = self.win.specs[tid].choices.items()[0]
        cfg.update(record=3, options={tid: {"levels": [boss], "mode": "high"}})
        self.win.current = 0
        self.win.refresh_summaries()
        self.assertEqual(self.win.summaries[tid].text(), f"存档 3 · 高档 · {name}")
        dlg = ChoiceDialog(self.win, self.win.specs[tid], acc, int(cfg["record"]), cfg)
        self.addCleanup(lambda: (dlg.close(), dlg.deleteLater()))
        self.assertIsNotNone(dlg.mode)

    def test_weekly_selected_world_boss_uses_its_configured_wide_save(self):
        acc = self.store.accounts[0]
        cfg = self.store.wide_cfg("u", boss_raid.WORLD_ID)
        cfg.update(record=5, options={boss_raid.WORLD_ID: {"levels": [960101], "times": 2, "mode": "mid"}})
        self.win.current, self.win._slot = 0, 2

        with mock.patch.object(self.win, "_launch") as launch:
            self.win.start(tasks=[boss_raid.WORLD_ID], here=True)

        accounts = launch.call_args.args[0]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].config.record_index, 4)
        self.assertEqual(accounts[0].tasks, (boss_raid.WORLD_ID,))
        self.assertEqual(accounts[0].options[boss_raid.WORLD_ID]["times"], 2)
        del acc

    def test_wide_timer_uses_its_selected_slot_and_task_switch_is_independent(self):
        self.store.data["global"] = {"tongtian_hire": {"on": True, "session": "both", "delay": 0, "accounts": {"u": {"on": True}}}}
        self.store.data["wide"] = {"u": {"tongtian_hire": {"on": False, "record": 5, "options": {"tongtian_hire": {"kind": 2}}}}}
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
        account = launch.call_args.args[0][0]
        self.assertEqual(account.config.record_index, 4)
        self.assertEqual(account.tasks, ("tongtian_hire",))
        self.assertEqual(account.options["tongtian_hire"], {"kind": 2})

    def test_world_boss_one_shot_toggle_resets_only_after_final_result(self):
        acc = self.store.accounts[0]
        cfg = self.store.wide_cfg("u", "world_boss", self.win.wide["world_boss"])
        cfg.update(record=2, options={"world_boss": {"levels": [960101], "skip_gold": True, "mode": "mid"}})
        self.win._index_targets()
        name = self.store.wide_name(self.store.label(acc), 2)

        self.win._on_result((name, core.TaskResult("world_boss", core.PROGRESS, "检查中")))
        self.assertTrue(cfg["options"]["world_boss"]["skip_gold"])
        self.win._on_result((name, core.TaskResult("world_boss", core.SUBMITTED, "完成")))

        self.assertEqual(cfg["options"]["world_boss"], {
            "levels": [960101], "skip_gold": False, "mode": "mid",
        })
        persisted = store.Store(self.store.path)
        self.assertFalse(persisted.wide_options("u", ["world_boss"])["world_boss"]["skip_gold"])

    def _arm_global(self, item):
        from zmxy import globalcfg, servertime
        self.store.data["global"] = {}
        globalcfg.section(self.store, item).update(on=True, at=servertime.clock().strftime("%H:%M"))
        return globalcfg

    def test_alliance_uses_page_slot(self):
        """联盟贡献定时只选账号，存档用页面“其他”里它自己选的。"""
        globalcfg = self._arm_global("alliance")
        self.store.data["wide"] = {"u": {"alliance": {"on": False, "record": 7}}}  # 页面开关不管，存档用它的
        globalcfg.entry(self.store, "alliance", "u")["on"] = True
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
            self.win._tick_timers()  # 同一分钟再看：不重跑
        self.assertEqual(launch.call_count, 1)
        [account] = launch.call_args.args[0]
        self.assertEqual((account.tasks, account.config.record_index), (("alliance",), 6))
        self.assertIn("定时·联盟贡献", launch.call_args.args[1])

    def test_daily_runs_page_ticks_of_picked_saves(self):
        """“日常和活动”：选中的存档做它在日常 / 活动页勾选的；周常、有自己定时项的任务、联盟贡献定时接管的跳过；账号级任务每账号一次。"""
        globalcfg = self._arm_global(globalcfg_mod().DAILY)
        saves = self.store.accounts[0]["saves"]
        saves["1"] = {"tasks": ["fulushou", "pvp_match", "ancient_wilderness"], "options": {"fulushou": {"levels": [1]}}}
        saves["3"] = {"tasks": ["growth_road", "wish_pool", "fulushou"]}  # 福禄寿没选关：不做（规则 1）
        saves["4"] = {"tasks": ["mystery_shop"]}  # 没选
        self.store.data["wide"] = {"u": {"vip_gift": {"on": True, "record": 2}, "world_boss": {"on": True, "record": 2},
                                         "alliance": {"on": True, "record": 2}}}
        globalcfg.entry(self.store, globalcfg.DAILY, "u")["slots"] = [1, 3, 5]  # 5 没勾选任务
        globalcfg.section(self.store, "alliance").update(on=True, at="00:00")
        globalcfg.entry(self.store, "alliance", "u")["on"] = True  # 联盟贡献归它自己的定时
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
            self.win._tick_timers()  # 同一账号一次只开始一个：联盟贡献先，下一轮日常和活动
        calls = {c.args[1]: c.args[0] for c in launch.call_args_list}
        daily = next(v for k, v in calls.items() if "日常和活动" in k)
        self.assertEqual([(a.config.record_index + 1, a.tasks) for a in daily],
                         [(2, ("vip_gift",)), (1, ("fulushou",)), (3, ("growth_road", "wish_pool"))])

    def test_weekly_reads_counts_then_runs(self):
        """“周常”：选中的星期到点，先自动配置（只读），再执行次数大于 0 的；读到的次数存回存档；世界Boss有自己的定时项，不在这里做。"""
        globalcfg = self._arm_global(globalcfg_mod().WEEKLY)
        globalcfg.section(self.store, globalcfg.WEEKLY)["weekdays"] = list(range(7))
        globalcfg.entry(self.store, globalcfg.WEEKLY, "u")["slots"] = [2]
        self.store.accounts[0]["saves"]["2"]["options"].update(demon_tower={"levels": [1]}, fairy_flag_shop={"shop_ids": [1]},
                                                                 ancient_wilderness={"levels": []})  # 上古蛮荒没选关：读到次数也不做
        self.store.accounts[0]["saves"]["2"]["weekly"] = ["ancient_wilderness", "demon_tower", "fairy_flag_shop", "star_maze"]  # 限时抢购没打开
        read = {"ancient_wilderness": 4, "demon_tower": 3, "star_maze": 0, last_rewards.ID: ("已领", False, {}, 1)}
        with mock.patch.object(core, "read_auto", return_value=read) as reader, mock.patch.object(self.win, "_work") as work:
            self.win._tick_timers()
            self.drain()
        reader.assert_called_once()
        self.assertNotIn(boss_raid.WORLD_ID, [s.id for s in reader.call_args.args[1]])
        self.assertFalse(reader.call_args.args[2])  # 没开按养成周：不留次数
        [accounts] = [c.args[0] for c in work.call_args_list]
        self.assertEqual([(a.config.record_index + 1, a.tasks) for a in accounts], [(2, ("demon_tower", "fairy_flag_shop"))])  # 两个商店没有次数框，照手动规则每次都去
        self.assertEqual(accounts[0].options["demon_tower"]["times"], 3)
        self.assertEqual(self.store.accounts[0]["saves"]["2"]["options"]["star_maze"]["times"], 0)

    def _at(self, h, m):
        from datetime import datetime
        from zmxy import servertime
        self.enterContext(mock.patch.object(servertime, "clock", return_value=datetime(2026, 11, 10, h, m)))

    def _arm_weekly_growth(self):
        globalcfg = self._arm_global(globalcfg_mod().WEEKLY)
        cfg = globalcfg.section(self.store, globalcfg.WEEKLY)
        cfg.update(growth=True, weekdays=[1], at="00:00")
        globalcfg.entry(self.store, globalcfg.WEEKLY, "u")["slots"] = [2]
        return globalcfg

    def test_weekly_growth_before_8_does_not_check_yet(self):
        """按养成周：新的养成周照表开始了、游戏还没更新，8 点以前不去看线上版本，约到 8 点。"""
        from zmxy import online
        self._at(0, 15)
        globalcfg = self._arm_weekly_growth()
        with mock.patch.object(globalcfg, "waiting_update", return_value=True), \
                mock.patch.object(online, "growth_week") as week, mock.patch.object(self.win, "_spawn") as spawn:
            self.win._tick_timers()
        week.assert_not_called()
        spawn.assert_not_called()
        check = globalcfg.section(store.Store(self.store.path), globalcfg.WEEKLY)["check"]
        self.assertEqual(check["at"], 8 * 60)
        self.assertIn("08:00 起每 30 分钟看一次", self.log.read_text(encoding="utf-8"))

    def test_weekly_growth_waits_for_update_then_runs(self):
        from zmxy import netcheck, online
        self._at(10, 0)
        globalcfg = self._arm_weekly_growth()
        self.enterContext(mock.patch.object(netcheck, "reachable", return_value=True))
        with mock.patch.object(globalcfg, "waiting_update", return_value=True), mock.patch.object(online, "growth_week", return_value=4), \
                mock.patch.object(self.win, "_spawn") as spawn:
            self.win._tick_timers()
            self.drain_growth()
        spawn.assert_not_called()
        self.assertIn("check", globalcfg.section(store.Store(self.store.path), globalcfg.WEEKLY))  # 关掉程序后台也能接着看
        with mock.patch.object(globalcfg, "waiting_update", side_effect=[True, False]), mock.patch.object(online, "growth_week", return_value=1), \
                mock.patch.object(self.win, "_spawn") as spawn:
            globalcfg.section(self.store, globalcfg.WEEKLY)["check"]["at"] = 0  # 约好的时间到了
            self.win._tick_timers()
            self.drain_growth()
        spawn.assert_called_once()  # 更新了：周常照常开始

    def drain_growth(self):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                kind, data = self.win.events.get(timeout=0.02)
            except queue.Empty:
                continue
            getattr(self.win, f"_on_{kind}")(data)
            if kind == "growth_check":
                return
        self.fail("没有等到更新检查的结果")

    def test_waits_for_network_before_catching_up(self):
        """开机刚登录还没联网：到点的先留着，不去登录；联网后再做。"""
        from zmxy import netcheck
        self.win._net_ok_until = 0.0
        with mock.patch.object(netcheck, "reachable", return_value=False), mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
            self.drain_net()
        launch.assert_not_called()
        self.assertEqual(len(self.win.timer_queue), 1)
        self.assertIn("网络还没连上", self.log.read_text(encoding="utf-8"))
        self.win._net_checked = -1e9  # 不等 15 秒
        with mock.patch.object(netcheck, "reachable", return_value=True), mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
            self.drain_net()
        self.assertEqual(launch.call_args.args[0][0].tasks, ("pvp_match",))
        self.assertEqual(self.win.timer_queue, [])
        self.assertIn("网络已连上", self.log.read_text(encoding="utf-8"))

    def test_gives_up_after_long_offline(self):
        self.win._net_ok_until = 0.0
        self.win.timer_queue = [(self.store.accounts[0], 2, "garden")]
        self.win._net_wait_since = time.monotonic() - 3601
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
        launch.assert_not_called()
        self.assertEqual(self.win.timer_queue, [])
        self.assertIn("网络一直没连上", self.log.read_text(encoding="utf-8"))

    def test_clock_item_retries_later_after_long_offline(self):
        globalcfg = self._arm_global("alliance")
        globalcfg.entry(self.store, "alliance", "u")["on"] = True
        self.win._net_ok_until = 0.0
        self.win._net_wait_since = time.monotonic() - 3601
        with mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()  # 到点、但网络等太久：放弃这一轮
        launch.assert_not_called()
        self.assertIn("之后再试", self.log.read_text(encoding="utf-8"))
        self.assertEqual(globalcfg.due(store.Store(self.store.path)), ["alliance"])  # 没算今天到过点

    def drain_net(self):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                kind, data = self.win.events.get(timeout=0.02)
            except queue.Empty:
                continue
            getattr(self.win, f"_on_{kind}")(data)
            if kind == "net":
                return
        self.fail("没有等到联网检查的结果")

    def test_busy_account_waits_and_other_account_can_start(self):
        self.store.accounts.append({"username": "v", "name": "另一个账号", "saves": {"1": {}}})
        self.store.data["global"]["pvp_match"]["accounts"]["v"] = {"slots": [1]}
        with mock.patch.object(self.win, "_busy", side_effect=lambda user: user == "u"), \
                mock.patch.object(core, "hosting", return_value=None), mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
        self.assertEqual(len(self.win.timer_queue), 1)
        self.assertEqual(launch.call_args.args[0][0].config.username, "v")
        with mock.patch.object(self.win, "_busy", return_value=False), mock.patch.object(self.win, "_launch") as launch:
            self.win._tick_timers()
        self.assertEqual(launch.call_args.args[0][0].config.username, "u")
        self.assertEqual(self.win.timer_queue, [])

    def test_background_captcha_returns_immediately_and_pause_does_not_open_dialog(self):
        self.assertIsNone(self.win._ask_captcha(b"image"))
        self.assertTrue(self.win.events.empty())
        with mock.patch.object(events, "notice") as notice:
            self.win._on_paused({})
        notice.assert_not_called()

    def test_closing_window_keeps_queued_timer_alive(self):
        self.win.background = False
        self.win.timer_queue.append((self.store.accounts[0], 2, "garden"))
        self.win.show()
        self.win.close()
        self.assertTrue(self.win.background)
        self.assertFalse(self.win.isVisible())
        self.assertFalse(self.app.quitOnLastWindowClosed())

    def test_idle_background_instance_exits_after_queue_is_empty(self):
        self.win._timer_checked = time.monotonic()
        with mock.patch.object(self.win, "_tick_timers"), mock.patch.object(self.app, "quit") as quit_:
            self.win._pump()
        quit_.assert_called_once_with()

    def test_registration_failure_is_visible_and_is_not_claimed_as_enabled(self):
        self.win._system_timers = True
        with mock.patch.object(scheduling, "register", side_effect=OSError("拒绝访问")):
            self.win._sync_schedule()
            self.drain()
        self.win.show_global()  # 定时页顶上显示
        self.assertIn("登记失败", self.win.schedule_status.text())
        self.assertIn("关闭程序后无法执行", self.win.schedule_status.text())
        self.assertIn("拒绝访问", self.log.read_text(encoding="utf-8"))

    def test_background_still_requires_existing_license_without_activation_dialog(self):
        from zmxy import license, license_ui, tablesync
        with mock.patch.object(license, "default") as default, mock.patch.object(license_ui, "ensure_licensed") as ask, \
                mock.patch.object(tablesync, "sync_at_start") as sync:
            default.return_value.check.return_value = False
            gui._main_window(self.app, True, [], [], mock.Mock())
        ask.assert_not_called()
        sync.assert_not_called()
        self.assertIn("授权失效", self.log.read_text(encoding="utf-8"))


SEND_SHOW = """
import pathlib, sys
from PySide6.QtCore import QCoreApplication
from zmxy.instance import Instance
app = QCoreApplication([])
sys.exit(0 if Instance(pathlib.Path(sys.argv[1])).send("show") else 1)
"""


@unittest.skipIf(QApplication is None, "未安装 PySide6")
class InstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_second_instance_cannot_take_lock_and_can_notify_first(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            first, second = Instance(path), Instance(path)
            received = []
            try:
                self.assertTrue(first.acquire())
                first.listen(received.append)
                self.assertFalse(second.acquire())
                # send 要等第一实例处理完才返回，而第一实例要在这里转事件循环：和真实场景一样放到另一个进程发
                self.app.processEvents()  # 先清掉前面界面测试留下的事件：第一轮要跑一两秒，send 只等对方 1 秒
                sender = subprocess.Popen([sys.executable, "-c", SEND_SHOW, str(path)], cwd=Path(__file__).parents[1])
                deadline = time.monotonic() + 15
                while sender.poll() is None and time.monotonic() < deadline:
                    self.app.processEvents()
                sender.kill()
                self.assertEqual(sender.wait(), 0)
                self.assertEqual(received, ["show"])
                first.close()
                self.assertTrue(second.acquire())
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()


def globalcfg_mod():
    from zmxy import globalcfg
    return globalcfg
