"""联盟仓库：单选材料、出售补货；Session.other_save。全部离线。"""

import contextlib
import unittest
from unittest import mock

from zmxy import login, protocol as p, state
from zmxy.core import Session, UncertainOutcome, load_tasks
from tests.support import FakeConn, day, run_daily as run

ITEM = 101045121  # 异鲲兽骨，半价货币 101026013，原价 40
OPT = {"materials": [ITEM]}


def info(bag=40, used=0, own=0):
    items = [{"id": 101026013, "count": bag}] + ([{"id": ITEM, "count": own}] if own else [])
    return {"bag": {"item": items}, **day(leagueBuyItemCnt=used)}


def league():
    return mock.patch.multiple(state, league=lambda i: {"id": 7}, league_id=lambda lg: 7)


def current_slot(n=1):
    return mock.patch.object(Session, "slot", new_callable=mock.PropertyMock, return_value=n)


def warehouse(n):
    return {"league.leagueHandler.getLeagueWarehouse": {"warehouse": {str(ITEM): n}}}


class ChoiceTests(unittest.TestCase):
    def test_single_material_without_map_name(self):
        ch = load_tasks()["union_warehouse"].choices
        self.assertEqual(ch.limit, 1)
        names = [n for _, n in ch.items()]
        self.assertTrue(names and all("（" not in n for n in names))
        self.assertEqual(ch.option, "materials")


class BuyTests(unittest.TestCase):
    def test_buys_two_when_stocked(self):
        conn = FakeConn(warehouse(3))
        with league():
            r = run("union_warehouse", conn, info(), OPT)
        self.assertEqual(r.status, "submitted")
        self.assertEqual(conn.bodies("scene.leagueHandler.buyItem"), [{"itemId": ITEM, "count": 2, "type": "whBuy", "buyType": 0, "password": ""}])

    def test_guards_do_not_buy(self):
        for i, stock in ((info(), 1), (info(bag=39), 5), (info(used=1), 5)):
            conn = FakeConn(warehouse(stock))
            with league():
                run("union_warehouse", conn, i, OPT)
            self.assertNotIn("scene.leagueHandler.buyItem", conn.routes())

    def test_selection_and_counter(self):
        self.assertEqual(run("union_warehouse", FakeConn(), info()).status, "skipped")
        bad = info()
        del bad["everyDaySetting"]
        self.assertEqual(run("union_warehouse", FakeConn(), bad, OPT).status, "blocked")
        self.assertEqual(run("union_warehouse", FakeConn(), info(), {"materials": [123]}).status, "blocked")

    def test_low_stock_without_sellers_skips(self):
        conn = FakeConn(warehouse(1))
        with league():
            r = run("union_warehouse", conn, info(), OPT)
        self.assertEqual(r.status, "skipped")


class RestockTests(unittest.TestCase):
    def setUp(self):
        self.visited = []

    def fixture(self, stock_now, own):
        counter = {"n": stock_now}

        def read(_body):
            return {"warehouse": {str(ITEM): counter["n"]}}

        def sell(body):
            counter["n"] += body["count"]
            return {}

        main = FakeConn({"league.leagueHandler.getLeagueWarehouse": read})
        seller_conn = FakeConn({"league.leagueHandler.getLeagueWarehouse": read, "scene.leagueHandler.sellItem": sell})
        seller = Session(p.ClientConfig(uid=1, username="u"))
        seller._conn, seller.info = seller_conn, info(own=own)
        seller.bind(load_tasks()["union_warehouse"])

        @contextlib.contextmanager
        def other_save(_self, slot):
            self.visited.append(slot)
            yield seller

        return main, seller_conn, other_save

    def go(self, stock_now, own, tweak=None):
        main, seller_conn, other = self.fixture(stock_now, own)
        if tweak:
            tweak(seller_conn)
        with league(), current_slot(), mock.patch.object(Session, "other_save", other), \
                mock.patch.object(Session, "refresh", lambda s: s.info):
            r = run("union_warehouse", main, info(), {**OPT, "sellers": [2]})
        return r, main, seller_conn

    def test_restock_then_buy(self):
        r, main, seller = self.go(0, 5)
        self.assertEqual(r.status, "submitted", r.detail)
        self.assertEqual(seller.bodies("scene.leagueHandler.sellItem"), [{"itemId": ITEM, "count": 2, "type": "whSell", "buyType": -1, "password": ""}])
        self.assertEqual(len(main.bodies("scene.leagueHandler.buyItem")), 1)
        self.assertEqual(self.visited, [2, 2])  # 先核对持有量，再出售

    def test_restock_refresh_rechecks_half_price_allowance_before_buying(self):
        for used in (1, 2, None):
            with self.subTest(used=used):
                main, seller, other = self.fixture(0, 5)

                def refresh(session):
                    session.info = info(used=used)
                    return session.info

                with league(), current_slot(), mock.patch.object(Session, "other_save", other), \
                        mock.patch.object(Session, "refresh", refresh):
                    result = run("union_warehouse", main, info(), {**OPT, "sellers": [2]})
                self.assertEqual(result.status, "blocked")
                self.assertTrue(seller.bodies("scene.leagueHandler.sellItem"))
                self.assertEqual(main.bodies("scene.leagueHandler.buyItem"), [])

    def test_sells_only_what_is_missing(self):
        _, _, seller = self.go(1, 9)
        self.assertEqual(seller.bodies("scene.leagueHandler.sellItem")[0]["count"], 1)

    def test_not_enough_in_sellers_sells_nothing(self):
        r, main, seller = self.go(0, 1)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(seller.bodies("scene.leagueHandler.sellItem"), [])
        self.assertEqual(main.bodies("scene.leagueHandler.buyItem"), [])

    def test_unconfirmed_sale_stops_purchase(self):
        r, main, _ = self.go(0, 5, lambda c: c.responses.__setitem__("scene.leagueHandler.sellItem", {}))
        self.assertEqual(r.status, "blocked")
        self.assertEqual(main.bodies("scene.leagueHandler.buyItem"), [])

    def test_sale_timeout_is_uncertain_and_never_buys(self):
        def tweak(conn):
            orig = conn.request

            def request(route, body):
                if route.endswith("sellItem"):
                    raise TimeoutError("t")
                return orig(route, body)

            conn.request = request

        main, seller_conn, other = self.fixture(0, 5)
        tweak(seller_conn)
        with league(), current_slot(), mock.patch.object(Session, "other_save", other):
            with self.assertRaises(UncertainOutcome):
                run("union_warehouse", main, info(), {**OPT, "sellers": [2]})
        self.assertEqual(main.bodies("scene.leagueHandler.buyItem"), [])

    def test_cannot_sell_for_currency_shortfall(self):
        """钱不够时不能先出售再发现买不起。"""
        main, seller_conn, other = self.fixture(0, 5)
        with league(), current_slot(), mock.patch.object(Session, "other_save", other):
            r = run("union_warehouse", main, info(bag=1), {**OPT, "sellers": [2]})
        self.assertEqual(r.status, "skipped")
        self.assertEqual(seller_conn.sent, [])
        self.assertEqual(self.visited, [])


class OtherSaveTests(unittest.TestCase):
    def make(self):
        s = Session(p.ClientConfig(uid=1, username="u", record_index=0))
        s._conn, s._link, s.info = mock.MagicMock(), mock.MagicMock(), {"x": 1}
        return s

    def test_switches_then_reconnects(self):
        s = self.make()
        first = s._conn
        logins = []

        def connect(config, on_cookie=None):
            logins.append(config.record_index)
            return mock.MagicMock(config=config)

        with mock.patch.object(login, "connect", connect), \
                mock.patch.object(login, "enter", lambda link: {"slot": link.config.record_index}):
            with s.other_save(3) as other:
                first.__exit__.assert_called_once()  # 先断开当前存档
                self.assertEqual(other.info, {"slot": 2})
            self.assertEqual(logins, [2, 0])
            self.assertEqual(s.info, {"slot": 0})

    def test_failed_reconnect_does_not_mask_original_error(self):
        s = self.make()
        calls = []

        def connect(config, on_cookie=None):
            calls.append(config.record_index)
            if len(calls) == 2:
                raise p.AllianceError("回连失败")
            return mock.MagicMock(config=config)

        with mock.patch.object(login, "connect", connect), mock.patch.object(login, "enter", lambda link: {}):
            with self.assertRaises(UncertainOutcome):
                with s.other_save(3):
                    raise UncertainOutcome("x")
        self.assertEqual(calls, [2, 0])

    def test_failed_reconnect_is_reported_when_block_succeeded(self):
        s = self.make()
        calls = []

        def connect(config, on_cookie=None):
            calls.append(1)
            if len(calls) == 2:
                raise p.AllianceError("回连失败")
            return mock.MagicMock(config=config)

        with mock.patch.object(login, "connect", connect), mock.patch.object(login, "enter", lambda link: {}):
            with self.assertRaises(p.AllianceError):
                with s.other_save(3):
                    pass


if __name__ == "__main__":
    unittest.main()
