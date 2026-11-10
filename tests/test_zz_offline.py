"""放在最后跑（文件名排在最后）：整套测试里不应该有任何用例真的去连外网。"""

import unittest

import tests


class OfflineTests(unittest.TestCase):
    def test_no_test_tried_to_reach_the_network(self):
        self.assertEqual(tests.BLOCKED, [], "有用例漏了 patch，试图联网（见 tests/__init__.py）")


if __name__ == "__main__":
    unittest.main()
