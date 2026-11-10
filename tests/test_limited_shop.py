"""验证周常限时抢购只读取有效灵魂商品并遵守库存、限购和余额。"""

import unittest
from unittest import mock

from zmxy import core
from zmxy.core import UncertainOutcome
from zmxy.tasks import limited_shop as shop
from tests.support import Fake, run, ts


def offer(shop_id, name, cost=20000, *, start="2026/10/9-00:00:00", end="2026/10/19-23:59:59",
          preview="2026/10/9-00:00:00", maximum=1, currency="spirit", role=0, order=1):
    return {
        "id": shop_id, "name": name, "type": 1, "order": order,
        "consume_type": "coin", "consume_subType": currency, "consume_count": cost,
        "sell_type": "item", "sell_subType": 101001001, "sell_count": "1",
        "previewTime": preview, "shelfTime": start, "underTime": end,
        "purchaseMaxNum": maximum, "purchaseMaxNum_double": maximum, "roleConditions": role,
    }


class LimitedShopTests(unittest.TestCase):
    NOW = ts(2026, 10, 15, 12)

    def setUp(self):
        self.rows = [
            offer(9001, "法宝洗炼石", order=1),
            offer(9002, "周年礼包", cost=10000, order=2),
            offer(9003, "点券礼包", currency="ticket", order=0),
            offer(9006, "元宝礼包", currency="yuanbao", order=0),
            offer(9004, "已下架商品", end="2026/10/12-23:59:59"),
            offer(9005, "角色限定商品", role=10),
        ]
        self.table = mock.patch.object(shop.state, "table_rows", return_value=self.rows)
        self.table.start()
        self.welfare = mock.patch.object(shop.state, "row", return_value={"welfareId": 21})
        self.welfare.start()

    def tearDown(self):
        self.table.stop()
        self.welfare.stop()

    def test_buys_only_current_soul_offers_and_uses_client_protocol(self):
        balance = {"value": 50000}

        def server(route, body):
            if route == shop.R_CLAIM_GIFT:
                self.assertEqual(body, {"giftId": 21, "giftKey": "SpeedDevelop"})
                return {"actFreeGifts": {"SpeedDevelop": {"21": 1}}}
            if route == shop.R_INFO:
                self.assertEqual(body, {"shopIds": [9001, 9002]})
                return {"data": {"9001": 5, "9002": 5}}
            self.assertEqual(route, shop.R_BUY)
            balance["value"] -= body["count"] * (20000 if body["shopId"] == 9001 else 10000)
            return {"spirit": balance["value"], "shopId": body["shopId"], "remain": 4}

        info = {"spirit": 50000, "shop": {}}
        conn = Fake(server)
        result = run("limited_time_shop", conn, info, now=self.NOW)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(conn.bodies("buyGlobalLimitShop"), [
            {"shopId": 9001, "count": 1}, {"shopId": 9002, "count": 1},
        ])
        self.assertEqual(conn.bodies("getActFreeGift"), [{"giftId": 21, "giftKey": "SpeedDevelop"}])
        self.assertIn("法宝洗炼石×1", result.detail)
        self.assertIn("周年礼包×1", result.detail)

    def test_respects_server_counts_and_per_account_cap(self):
        rows = [offer(9001, "商品一", maximum=2), offer(9002, "商品二", maximum=1)]
        with mock.patch.object(shop.state, "table_rows", return_value=rows):
            def server(route, body):
                if route == shop.R_CLAIM_GIFT:
                    return {}
                if route == shop.R_INFO:
                    return {"data": {"9001": 8, "9002": 8}}
                return {"shopId": body["shopId"], "remain": 7}

            conn = Fake(server)
            result = run("limited_time_shop", conn,
                         {"spirit": 50000, "everyWeekSetting": {"shopBuyCount": {"9001": {"buyCount": 1}}},
                          "shop": {"9002": {"buyCount": 1}}}, now=self.NOW)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(conn.bodies("buyGlobalLimitShop"), [{"shopId": 9001, "count": 1}])
        self.assertIn("商品一×1", result.detail)
        self.assertIn("商品二已达到账号限购", result.detail)

    def test_missing_stock_blocks_that_item_without_guessing(self):
        def server(route, body):
            if route == shop.R_CLAIM_GIFT:
                return {}
            return {"data": {}} if route == shop.R_INFO else {}

        conn = Fake(server)
        result = run("limited_time_shop", conn, {"spirit": 50000}, now=self.NOW)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertFalse(conn.bodies("buyGlobalLimitShop"))
        self.assertIn("库存数量缺失", result.detail)

    def test_insufficient_spirit_skips_purchase(self):
        conn = Fake(lambda route, body: {"data": {"9001": 1, "9002": 1}} if route == shop.R_INFO else {})
        result = run("limited_time_shop", conn, {"spirit": 9999}, now=self.NOW)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertFalse(conn.bodies("buyGlobalLimitShop"))
        self.assertIn("灵魂不足", result.detail)

    def test_unknown_server_time_blocks_without_query_or_purchase(self):
        conn = Fake()
        result = run("limited_time_shop", conn, {"spirit": 50000}, now=None)
        self.assertEqual(result.status, "blocked", result.detail)
        self.assertEqual(conn.bodies("getActFreeGift"), [{"giftId": 21, "giftKey": "SpeedDevelop"}])
        self.assertFalse(conn.bodies("getLimitShopInfo"))
        self.assertFalse(conn.bodies("buyGlobalLimitShop"))

    def test_uncertain_purchase_is_not_retried(self):
        def server(route, body):
            if route == shop.R_CLAIM_GIFT:
                return {}
            if route == shop.R_INFO:
                return {"data": {"9001": 1, "9002": 1}}
            raise TimeoutError("lost response")

        conn = Fake(server)
        with self.assertRaises(UncertainOutcome):
            run("limited_time_shop", conn, {"spirit": 50000}, now=self.NOW)
        self.assertEqual(conn.routes().count("buyGlobalLimitShop"), 1)

    def test_later_uncertain_purchase_reports_confirmed_prior_buys(self):
        calls = []

        def server(route, body):
            if route == shop.R_CLAIM_GIFT:
                return {}
            if route == shop.R_INFO:
                return {"data": {"9001": 1, "9002": 1}}
            calls.append(body["shopId"])
            if len(calls) == 2:
                raise TimeoutError("lost response")
            return {"shopId": body["shopId"], "remain": 0}

        from tests.support import Fake
        conn = Fake(server)
        with self.assertRaisesRegex(UncertainOutcome, "法宝洗炼石"):
            run("limited_time_shop", conn, {"spirit": 50000}, now=self.NOW)
        self.assertEqual(conn.routes().count("buyGlobalLimitShop"), 2)

    def test_task_is_registered_as_a_weekly_purchase_with_guarded_route(self):
        spec = core.load_tasks()[shop.ID]
        self.assertEqual(spec.group, "周常")
        self.assertEqual(spec.writes, frozenset({shop.R_BUY, shop.R_CLAIM_GIFT}))
        self.assertEqual(spec.spends, frozenset({shop.R_BUY}))

    def test_claims_welfare_even_when_there_are_no_active_shop_offers(self):
        with mock.patch.object(shop.state, "table_rows", return_value=[]):
            conn = Fake(lambda route, body: {})
            result = run("limited_time_shop", conn, {"spirit": 50000}, now=self.NOW)
        self.assertEqual(result.status, "submitted", result.detail)
        self.assertEqual(conn.bodies("getActFreeGift"), [{"giftId": 21, "giftKey": "SpeedDevelop"}])
        self.assertFalse(conn.bodies("buyGlobalLimitShop"))

    def test_does_not_claim_welfare_again_when_auth_record_says_claimed(self):
        with mock.patch.object(shop.state, "table_rows", return_value=[]):
            conn = Fake()
            with mock.patch.object(shop, "_gift_claimed", return_value=True):
                result = run("limited_time_shop", conn, {"spirit": 50000}, now=self.NOW)
        self.assertEqual(result.status, "skipped", result.detail)
        self.assertFalse(conn.bodies("getActFreeGift"))
        self.assertIn("养成福利本周已领取", result.detail)


if __name__ == "__main__":
    unittest.main()
