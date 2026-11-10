"""命令行入口。全部离线。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zmxy import cli, store
from zmxy.core import load_tasks


class CliTests(unittest.TestCase):
    def test_list_tasks_prints_every_registered_task(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--list-tasks"]), 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), len(load_tasks()))
        self.assertTrue(any(line.startswith("vip_gift") for line in lines))

    def test_without_accounts_it_refuses_to_run(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(store, "settings_path", lambda: Path(tmp) / "settings.json"), \
                mock.patch.object(cli, "run_batch") as run, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 2)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
