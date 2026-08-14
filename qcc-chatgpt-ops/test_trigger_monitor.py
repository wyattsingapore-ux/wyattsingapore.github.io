import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chatgpt_trigger_monitor as m

ET = ZoneInfo("America/New_York")


def make_bars(base=100.0, breakout=None, volume=1000.0):
    start = datetime(2026, 8, 14, 10, 0, tzinfo=ET)
    out = []
    for i in range(35):
        p = base + (i % 4) * 0.02
        out.append(m.Bar(start + timedelta(minutes=i), p, p+0.05, p-0.05, p, volume))
    if breakout == "up":
        b = out[-1]
        out[-1] = m.Bar(b.dt, 100.05, 100.70, 100.04, 100.65, 2500)
    elif breakout == "down":
        b = out[-1]
        out[-1] = m.Bar(b.dt, 100.02, 100.03, 99.30, 99.35, 2500)
    return out


class TriggerTests(unittest.TestCase):
    def setUp(self):
        for k in ["TRIGGER_TSLA_UP", "TRIGGER_TSLA_DOWN", "TSLA_UP", "TSLA_DOWN"]:
            os.environ.pop(k, None)
        os.environ["CHATGPT_RVOL_THRESHOLD"] = "1.5"
        os.environ["CHATGPT_TRIGGER_LOOKBACK"] = "12"
        os.environ["CHATGPT_RVOL_BASELINE"] = "20"

    def test_auto_long_breakout(self):
        s = m.derive_snapshot("TSLA", make_bars(breakout="up"), datetime(2026, 8, 14, 10, 35, 5, tzinfo=ET))
        self.assertEqual(s.status, "LONG_CONFIRMED")

    def test_auto_short_breakout(self):
        s = m.derive_snapshot("TSLA", make_bars(breakout="down"), datetime(2026, 8, 14, 10, 35, 5, tzinfo=ET))
        self.assertEqual(s.status, "SHORT_CONFIRMED")

    def test_manual_levels(self):
        os.environ["TRIGGER_TSLA_UP"] = "101.0"
        os.environ["TRIGGER_TSLA_DOWN"] = "99.0"
        s = m.derive_snapshot("TSLA", make_bars(), datetime(2026, 8, 14, 10, 35, 5, tzinfo=ET))
        self.assertEqual(s.mode, "manual")
        self.assertAlmostEqual(s.up_trigger, 101.0)
        self.assertAlmostEqual(s.down_trigger, 99.0)

    def test_weekend_closed(self):
        self.assertFalse(m.is_regular_session(datetime(2026, 8, 15, 10, 0, tzinfo=ET)))


if __name__ == "__main__":
    unittest.main()
