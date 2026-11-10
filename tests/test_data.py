import unittest

from zmxy import data


class DataTests(unittest.TestCase):
    def test_base_files_exist(self):
        self.assertTrue(data.TABLE_ROOT.is_dir())
        self.assertTrue(data.CLIENT_SWF.is_file())

    def test_lookup_by_name_ignores_number_prefix_and_case(self):
        self.assertEqual(data.path_of("Dungeon"), data.path_of("0082_Dungeon.json"))
        self.assertEqual(data.path_of("dungeon"), data.path_of("Dungeon"))

    def test_table_and_row(self):
        self.assertGreater(len(data.table("Dungeon")), 100)
        self.assertEqual(data.row("Dungeon", 30720)["name"], "琼芝星树")
        self.assertIsNone(data.row("Dungeon", -1))

    def test_northern_abyss_rows_present_in_baseline(self):
        # 旧版需要从 0928 表叠加的记录，现在基础表本身就带有
        self.assertIsNotNone(data.row("Dungeon", 1400113))
        self.assertIsNotNone(data.row("Prop", 101045121))

    def test_missing_table(self):
        with self.assertRaises(data.DataError):
            data.table("NoSuchTable")

    def test_switching_version_drops_cached_tables_and_results_derived_from_them(self):
        import json, tempfile
        from pathlib import Path

        calls = []

        @data.cached
        def names() -> tuple:
            calls.append(1)
            return tuple(r["name"] for r in data.table("Demo").values())

        keep = (data.TABLE_ROOT, data.BASELINE_ROOT)
        self.addCleanup(lambda: (data._derived.remove(names), data.use(*keep)))
        with tempfile.TemporaryDirectory() as tmp:
            for version, name in (("a", "旧"), ("b", "新")):
                (Path(tmp) / version).mkdir()
                (Path(tmp) / version / "0001_Demo.json").write_text(json.dumps({"1": {"name": name}}), encoding="utf-8")
            data.use(Path(tmp) / "a")
            self.assertEqual((names(), names(), len(calls)), (("旧",), ("旧",), 1))
            self.assertIs(data.table("Demo"), data.table("Demo"))  # 第二次不再读盘
            data.use(Path(tmp) / "b")
            self.assertEqual((names(), data.row("Demo", 1)["name"]), (("新",), "新"))

    def test_slim_keeps_only_the_asked_columns_and_not_the_whole_table(self):
        names = data.slim("Skill", "name")
        self.assertEqual(set(next(iter(names.values()))), {"name"})
        self.assertNotIn((data.TABLE_ROOT, "Skill"), data._tables)  # 40MB 的整表没有留在内存里
        self.assertIs(data.slim("Skill", "name"), names)  # 第二次不再读盘


if __name__ == "__main__":
    unittest.main()
