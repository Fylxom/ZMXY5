"""源码全量归档、EXE禁止项目落盘及最近三份完成记录保留策略。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zmxy import run_records as records


class RecordRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root/'project'
        self.cache = self.root/'appdata'
        for patch in (mock.patch.object(records, 'PROJECT', self.project),
                      mock.patch.object(records, 'frozen', return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def boss(self, n):
        root = self.cache/'world_boss_log'
        root.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            p = root/f'battle-{i}.json'
            p.write_text(json.dumps({'time': f'{i}', 'boss': 1, 'players': [], 'outcome': 'done'}), encoding='utf-8')
            os.utime(p, (100+i, 100+i))
        return root

    def pets(self, completed=4, paused=2):
        root = self.cache/'pet_cultivation_runs'
        for i in range(completed+paused):
            run = f'{i:032x}'
            p = root/'owner'/f'gpt_{run}.json'
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({'version': 1, 'owner': 'owner', 'runId': run,
                                    'status': 'completed' if i < completed else 'paused', 'pending': None}), encoding='utf-8')
            os.utime(p, (100+i, 100+i))
        return root

    def test_source_archives_all_battles_and_keeps_three_recent_cache(self):
        root = self.boss(5)
        originals = {p.name: p.read_bytes() for p in root.glob('*.json')}
        result = records.maintain('boss', root)
        archive = self.project/'世界Boss/实战记录'
        self.assertEqual({p.name: p.read_bytes() for p in archive.glob('*.json')}, originals)
        self.assertEqual({p.name for p in root.glob('*.json')}, {'battle-2.json','battle-3.json','battle-4.json'})
        self.assertEqual(result['removed'], 2)

    def test_released_pet_sidecar_is_archived_and_removed_with_its_run(self):
        root = self.pets(completed=4, paused=1)
        for p in root.rglob('gpt_*.json'):
            p.with_name(p.stem + records.PET_RELEASED_SUFFIX).write_text('{"entityId":"a","record":{}}\n', encoding='utf-8')
        records.maintain('pet', root)
        archive = self.project/'宠物潜能研究/实战记录/owner'
        self.assertEqual(len(list(archive.glob('*' + records.PET_RELEASED_SUFFIX))), 5)  # 全部归档
        left = {p.stem for p in (root/'owner').glob('gpt_*.json')}
        sidecars = {p.name[:-len(records.PET_RELEASED_SUFFIX)] for p in (root/'owner').glob('*' + records.PET_RELEASED_SUFFIX)}
        self.assertEqual(sidecars, left)  # 清理掉的完成记录连同放生记录一起删，留下的都还在

    def test_packaged_never_creates_project_archive(self):
        root = self.boss(5)
        with mock.patch.object(records, 'frozen', return_value=True):
            result = records.maintain('boss', root)
        self.assertFalse(self.project.exists())
        self.assertEqual(len(list(root.glob('*.json'))), 3)
        self.assertEqual(result['archived'], 0)

    def test_paused_pets_are_extra_and_always_preserved(self):
        root = self.pets()
        result = records.maintain('pet', root)
        self.assertEqual(len(list((self.project/'宠物潜能研究/实战记录').rglob('gpt_*.json'))), 6)
        self.assertEqual(len(list(root.rglob('gpt_*.json'))), 5)
        self.assertEqual(result['unfinished'], 2)
        self.assertEqual(result['removed'], 1)

    def test_packaged_also_preserves_unfinished_checkpoint(self):
        root = self.pets()
        with mock.patch.object(records, 'frozen', return_value=True):
            records.maintain('pet', root)
        self.assertEqual(len(list(root.rglob('gpt_*.json'))), 5)
        self.assertFalse(self.project.exists())

    def test_archive_failure_never_deletes_originals(self):
        root = self.boss(5)
        with mock.patch.object(records, 'archive_file', side_effect=PermissionError('locked')):
            result = records.maintain('boss', root)
        self.assertEqual(len(list(root.glob('*.json'))), 5)
        self.assertEqual(result['errors'], 5)

    def test_changed_source_cannot_be_deleted_using_stale_snapshot(self):
        root = self.boss(5)
        archive = records.archive_file
        def racing(kind, base, file, payload=None):
            result = archive(kind, base, file, payload)
            if file.name == 'battle-0.json':
                file.write_bytes(file.read_bytes()+b'\n')
            return result
        with mock.patch.object(records, 'archive_file', side_effect=racing):
            records.maintain('boss', root)
        self.assertTrue((root/'battle-0.json').is_file())

    def test_invalid_json_and_temp_candidates_are_not_removed(self):
        root = self.boss(5)
        (root/'broken.json').write_text('{', encoding='utf-8')
        (root/'failed.tmp').write_text('recovery candidate', encoding='utf-8')
        records.maintain('boss', root)
        self.assertTrue((root/'broken.json').is_file())
        self.assertTrue((root/'failed.tmp').is_file())

    def test_archive_rejects_path_outside_cache(self):
        root = self.boss(1)
        outside = self.root/'outside.json'
        outside.write_text('{}', encoding='utf-8')
        with self.assertRaises(ValueError):
            records.archive_file('boss', root, outside)

    def test_runtime_pet_journal_mirrors_and_keeps_same_identity(self):
        from zmxy.tasks.pet_cultivation import journal
        root = self.cache/'pet_cultivation_runs'
        ctx = SimpleNamespace(session=SimpleNamespace(slot=1), config=SimpleNamespace(username='u', uid=1),
                              options={}, progress={})
        with mock.patch.object(journal, 'RUN_ROOT', root), \
             mock.patch.object(records, 'settings_path', return_value=self.cache/'settings.json'):
            run = journal.RunJournal(ctx)
            run.initialize(status='running', pending=None, field=None, harvested=0)
            archive = self.project/'宠物潜能研究/实战记录'/run.owner/run.path.name
            self.assertEqual(archive.read_bytes(), run.path.read_bytes())
            run.data['status'] = 'completed'
            run.save()
            self.assertEqual(json.loads(archive.read_text(encoding='utf-8'))['runId'], run.run_id)

    def test_maintenance_error_does_not_stop_gui_startup(self):
        with mock.patch.object(records, 'maintain', side_effect=PermissionError('unavailable')):
            results = records.maintain_all(self.cache)
        self.assertEqual(results['boss']['errors'], 1)
        self.assertEqual(results['pet']['errors'], 1)
