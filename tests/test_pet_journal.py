"""断点短暂占用重试、旧文件保护和写入屏障离线回归。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zmxy.tasks.pet_cultivation import journal as state


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ctx = SimpleNamespace(session=SimpleNamespace(slot=1), config=SimpleNamespace(username='offline', uid=1),
                              options={}, progress={})
        self.j = state.RunJournal(ctx, root=Path(self.temp.name))
        self.j.initialize(harvested=7, goal=8, pending={'kind': 'free', 'ack': True})

    def test_transient_replace_access_denied_retries_same_snapshot(self):
        replace = state.os.replace
        calls = []
        def locked_once(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise PermissionError('temporary lock')
            return replace(source, destination)
        self.j.data['harvested'] = 8
        with mock.patch.object(state.os, 'replace', side_effect=locked_once), mock.patch.object(state.time, 'sleep') as sleep:
            self.j.save()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        sleep.assert_called_once_with(state.REPLACE_DELAYS[0])
        self.assertEqual(json.loads(self.j.path.read_text(encoding='utf-8'))['harvested'], 8)

    def test_persistent_denial_keeps_old_json_and_candidate(self):
        old = self.j.path.read_bytes()
        self.j.data['harvested'] = 8
        with mock.patch.object(state.os, 'replace', side_effect=PermissionError('locked')) as replace, \
             mock.patch.object(state.time, 'sleep'):
            with self.assertRaises(PermissionError):
                self.j.save()
        self.assertEqual(replace.call_count, len(state.REPLACE_DELAYS)+1)
        self.assertEqual(self.j.path.read_bytes(), old)
        candidates = list(self.j.path.parent.glob('*.tmp'))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(json.loads(candidates[0].read_text(encoding='utf-8'))['harvested'], 8)

    def test_unchanged_snapshot_does_not_replace_again(self):
        with mock.patch.object(state.os, 'replace') as replace:
            self.j.save()
        replace.assert_not_called()

    def test_pending_and_ack_changes_are_never_skipped(self):
        replace = state.os.replace
        with mock.patch.object(state.os, 'replace', wraps=replace) as called:
            self.j.data['pending'] = {'kind': 'remove', 'ack': False}
            self.j.save()
            self.j.data['pending']['ack'] = True
            self.j.save()
            self.j.data['pending'] = None
            self.j.save()
        self.assertEqual(called.call_count, 3)
        self.assertIsNone(json.loads(self.j.path.read_text(encoding='utf-8'))['pending'])

    def test_cached_pet_json_matches_full_dump_and_detects_nested_changes(self):
        self.j.data['pets'] = {'新宠"一': {'phase': 'growing', 'latest': {
            'skill': {'passivity': {'8200110': 1}}, 'name': '天马\n一'}}}
        for change in ('initial', 'rank', 'receipt', 'delete', 'replace'):
            with self.subTest(change=change):
                record = self.j.data['pets'].get('新宠"一')
                if change == 'rank':record['latest']['skill']['passivity']['8200110'] = 3
                elif change == 'receipt':record['receipt'] = {'count': 1, 'level': 3}
                elif change == 'delete':del self.j.data['pets']['新宠"一']
                elif change == 'replace':self.j.data['pets']['新宠"一'] = {'phase': 'ready', 'initial': {}}
                expected = json.dumps(self.j.data, ensure_ascii=False, separators=(',', ':'))+'\n'
                self.assertEqual(self.j._payload(), expected)
                self.j.save()
                self.assertEqual(self.j.path.read_text(encoding='utf-8'), expected)

    def test_pending_barriers_remain_exact_with_cached_history(self):
        self.j.data['pets'] = {str(n): {'phase': 'growing', 'latest': {'level': n}} for n in range(30)}
        self.j.save()
        for pending in ({'kind': 'capture', 'ack': False}, {'kind': 'capture', 'ack': True}, None):
            self.j.data['pending'] = pending
            self.j.save()
            loaded = json.loads(self.j.path.read_text('utf-8'))
            self.assertEqual(loaded, self.j.data)
            self.assertEqual(loaded['pending'], pending)

    def _main(self):
        return json.loads(self.j.path.read_text(encoding='utf-8'))

    def test_released_records_move_to_sidecar_and_load_merges_them(self):
        self.j.data['pets'] = {'a': {'phase': 'released', 'n': 1}, 'b': {'phase': 'growing', 'n': 2}}
        self.j.save()
        self.assertEqual(list(self._main()['pets']), ['b'])
        side = state.released_path(self.j.path).read_text(encoding='utf-8').splitlines()
        self.assertEqual([json.loads(line)['entityId'] for line in side], ['a'])
        data, frozen = state.load_file(self.j.path)
        self.assertEqual(data['pets'], self.j.data['pets'])
        self.assertEqual(frozen, {'a'})
        self.j.data['pets']['b']['phase'] = 'released'
        self.j.save()
        self.j.save()  # 没有新放生的：不重复追加
        self.assertEqual(self._main()['pets'], {})
        self.assertEqual(len(state.released_path(self.j.path).read_text(encoding='utf-8').splitlines()), 2)
        self.assertEqual(state.load_file(self.j.path)[0]['pets'], self.j.data['pets'])

    def test_resumed_journal_keeps_full_pet_history_and_main_record_wins(self):
        self.j.data['pets'] = {'a': {'phase': 'released'}, 'b': {'phase': 'released'}}
        self.j.save()
        main = self._main()
        main['pets']['a'] = {'phase': 'released', 'note': '主断点为准'}  # 追加后、主断点替换前断电的样子
        self.j.path.write_text(json.dumps(main, ensure_ascii=False), encoding='utf-8')
        with state.released_path(self.j.path).open('a', encoding='utf-8') as stream:
            stream.write('{"entityId":"c","rec')  # 最后一行没写完
        ctx = SimpleNamespace(session=SimpleNamespace(slot=1), config=SimpleNamespace(username='offline', uid=1),
                              options={'resume_done': {state.PREFIX + self.j.run_id: 1}}, progress={})
        again = state.RunJournal(ctx, root=Path(self.temp.name))
        self.assertEqual(again.data['pets'], {'a': {'phase': 'released', 'note': '主断点为准'}, 'b': {'phase': 'released'}})
        again.data['pets']['b'] = {'phase': 'ready'}  # 整条换掉的记录回到主断点
        again.save()
        self.assertEqual(state.load_file(again.path)[0]['pets']['b'], {'phase': 'ready'})

    def test_corrupt_middle_line_refuses_to_resume(self):
        self.j.data['pets'] = {'a': {'phase': 'released'}}
        self.j.save()
        file = state.released_path(self.j.path)
        file.write_text('坏行\n' + file.read_text(encoding='utf-8'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '损坏'):
            state.load_file(self.j.path)

    def test_non_permission_error_does_not_retry(self):
        self.j.data['harvested'] = 8
        with mock.patch.object(state.os, 'replace', side_effect=OSError('disk full')) as replace:
            with self.assertRaises(OSError):
                self.j.save()
        self.assertEqual(replace.call_count, 1)

    def test_failed_temp_does_not_collide_with_next_attempt(self):
        self.j.data['harvested'] = 8
        with mock.patch.object(state.os, 'replace', side_effect=PermissionError('locked')), mock.patch.object(state.time, 'sleep'):
            with self.assertRaises(PermissionError):
                self.j.save()
        failed = next(self.j.path.parent.glob('*.tmp'))
        self.j.save()
        self.assertTrue(failed.is_file())
        self.assertEqual(json.loads(self.j.path.read_text(encoding='utf-8'))['harvested'], 8)
