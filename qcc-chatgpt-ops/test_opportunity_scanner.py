import os
import unittest
from datetime import datetime, timedelta

import chatgpt_opportunity_scanner as scan
import chatgpt_trigger_monitor as core


class OpportunityScannerTests(unittest.TestCase):
    def setUp(self):
        for key in [
            "CHATGPT_UNIVERSE_SYMBOLS",
            "CHATGPT_UNIVERSE_SCAN_MIN",
            "CHATGPT_RUNNER_SCAN_MIN",
            "QCC_TRADING_TELEGRAM_BOT_TOKEN",
            "QCC_TRADING_TELEGRAM_CHAT_ID",
        ]:
            os.environ.pop(key, None)

    def snap(self, symbol="TSLA", price=100, up=101, down=95, rv=1.0,
             e9=100.5, e21=99.5, atr=2.0, status="WAIT"):
        return core.SignalSnapshot(
            symbol=symbol, mode="auto-5m", observed_price=price,
            completed_dt="2026-08-14T10:00:00-04:00", completed_close=price,
            up_trigger=up, down_trigger=down, rel_volume=rv,
            ema9=e9, ema21=e21, atr14=atr, status=status,
        )

    def test_default_universe_has_14_symbols(self):
        expected = ["SPY","QQQ","TSLA","NVDA","MSTR","AVGO","AMD","META","AMZN","AAPL","GOOGL","MSFT","PLTR","COIN"]
        self.assertEqual(scan.universe_from_env(), expected)

    def test_dedicated_telegram_does_not_fall_back_to_jarvis(self):
        os.environ["TELEGRAM_BOT_TOKEN"] = "jarvis-token"
        os.environ["TELEGRAM_CHAT_ID"] = "jarvis-chat"
        tg = scan.trading_telegram()
        self.assertFalse(tg.configured)
        os.environ["QCC_TRADING_TELEGRAM_BOT_TOKEN"] = "trading-token"
        os.environ["QCC_TRADING_TELEGRAM_CHAT_ID"] = "trading-chat"
        self.assertTrue(scan.trading_telegram().configured)

    def test_confirmed_breakout_ranks_above_distant_wait(self):
        strong = scan.rank_snapshot(self.snap(price=101.5, up=101, rv=2.0, status="LONG_CONFIRMED"))
        weak = scan.rank_snapshot(self.snap(symbol="SPY", price=97, up=103, down=92, rv=0.7, e9=97, e21=97, atr=2))
        self.assertGreater(strong.score, weak.score)

    def test_near_trending_candidate_scores_well(self):
        near = scan.rank_snapshot(self.snap(price=100.9, up=101, rv=1.6, e9=100.8, e21=100.0, atr=1.0, status="NEAR_LONG"))
        far = scan.rank_snapshot(self.snap(symbol="QQQ", price=98, up=102, down=94, rv=0.8, e9=98, e21=98, atr=2.0))
        self.assertGreater(near.score, far.score)
        self.assertEqual(near.bias, "LONG")

    def test_late_signal_penalized(self):
        live = scan.rank_snapshot(self.snap(rv=2.0, status="LONG_CONFIRMED"))
        late = scan.rank_snapshot(self.snap(rv=2.0, status="LATE_LONG"))
        self.assertGreater(live.score, late.score)

    def test_full_scan_cadence_defaults_to_20_minutes(self):
        now = datetime(2026, 8, 14, 11, 0, tzinfo=core.ET)
        self.assertTrue(scan.due_full_scan(now, {}))
        state = {"last_universe_scan": (now - timedelta(minutes=19)).isoformat()}
        self.assertFalse(scan.due_full_scan(now, state))
        state["last_universe_scan"] = (now - timedelta(minutes=20)).isoformat()
        self.assertTrue(scan.due_full_scan(now, state))

    def test_runner_scan_cadence(self):
        now = datetime(2026, 8, 14, 11, 0, tzinfo=core.ET)
        state = {"last_runner_scan": (now - timedelta(minutes=4)).isoformat()}
        self.assertFalse(scan.due_runner_scan(now, state))
        state["last_runner_scan"] = (now - timedelta(minutes=5)).isoformat()
        self.assertTrue(scan.due_runner_scan(now, state))


if __name__ == "__main__":
    unittest.main()
