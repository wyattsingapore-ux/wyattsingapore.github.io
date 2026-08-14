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
    """Build 150 contiguous 1m bars = 30 complete 5m buckets.

    The final 5m bucket is optionally made a high-volume breakout so the
    hardened auto-5m production logic has enough history for EMA21, ATR14,
    RVOL20 and a 12-bar structure lookback.
    """
    start = datetime(2026, 8, 14, 10, 0, tzinfo=ET)
    out = []
    for i in range(150):
        p = base + (i % 4) * 0.02
        out.append(m.Bar(start + timedelta(minutes=i), p, p + 0.05, p - 0.05, p, volume))

    # Last complete bucket is 12:25-12:29. Raise volume on all five minutes so
    # its aggregated 5m RVOL is comfortably above 1.5x.
    if breakout in ("up", "down"):
        for idx in range(-5, 0):
            b = out[idx]
            out[idx] = m.Bar(b.dt, b.open, b.high, b.low, b.close, 2200.0)

    if breakout == "up":
        b = out[-1]
        out[-1] = m.Bar(b.dt, 100.05, 100.70, 100.04, 100.65, 2200.0)
    elif breakout == "down":
        b = out[-1]
        out[-1] = m.Bar(b.dt, 100.02, 100.03, 99.30, 99.35, 2200.0)
    return out


class TriggerTests(unittest.TestCase):
    def setUp(self):
        for k in [
            "TRIGGER_TSLA_UP", "TRIGGER_TSLA_DOWN", "TSLA_UP", "TSLA_DOWN",
            "CHATGPT_ENTRY_CUTOFF_ET", "CHATGPT_FINALIZATION_LAG_SEC",
        ]:
            os.environ.pop(k, None)
        os.environ["CHATGPT_RVOL_THRESHOLD"] = "1.5"
        os.environ["CHATGPT_TRIGGER_LOOKBACK"] = "12"
        os.environ["CHATGPT_RVOL_BASELINE"] = "20"
        os.environ["CHATGPT_FINALIZATION_LAG_SEC"] = "120"
        os.environ["CHATGPT_ENTRY_CUTOFF_ET"] = "15:30"

    def test_auto_long_breakout(self):
        now = datetime(2026, 8, 14, 12, 32, 5, tzinfo=ET)
        s = m.derive_snapshot("TSLA", make_bars(breakout="up"), now)
        self.assertEqual(s.status, "LONG_CONFIRMED")
        self.assertEqual(s.mode, "auto-5m")

    def test_auto_short_breakout(self):
        now = datetime(2026, 8, 14, 12, 32, 5, tzinfo=ET)
        s = m.derive_snapshot("TSLA", make_bars(breakout="down"), now)
        self.assertEqual(s.status, "SHORT_CONFIRMED")
        self.assertEqual(s.mode, "auto-5m")

    def test_manual_levels(self):
        os.environ["TRIGGER_TSLA_UP"] = "101.0"
        os.environ["TRIGGER_TSLA_DOWN"] = "99.0"
        now = datetime(2026, 8, 14, 12, 32, 5, tzinfo=ET)
        s = m.derive_snapshot("TSLA", make_bars(), now)
        self.assertEqual(s.mode, "manual-5m")
        self.assertAlmostEqual(s.up_trigger, 101.0)
        self.assertAlmostEqual(s.down_trigger, 99.0)

    def test_finalization_lag_excludes_fresh_bar(self):
        bars = make_bars()
        # At 12:30:30 ET with a 120s lag, the 12:29 bar (ending 12:30) is too
        # fresh to be finalized; the latest finalized bar must be <= 12:27.
        _, finalized = m.split_current_and_completed(
            bars, datetime(2026, 8, 14, 12, 30, 30, tzinfo=ET)
        )
        self.assertLessEqual(finalized[-1].dt, datetime(2026, 8, 14, 12, 27, tzinfo=ET))

    def test_late_breakout_is_informational(self):
        now = datetime(2026, 8, 14, 15, 37, 5, tzinfo=ET)
        # Shift the fixture to end just before 15:35 while preserving shape.
        bars = make_bars(breakout="up")
        delta = datetime(2026, 8, 14, 13, 5, tzinfo=ET) - bars[0].dt
        shifted = [m.Bar(b.dt + delta, b.open, b.high, b.low, b.close, b.volume) for b in bars]
        s = m.derive_snapshot("TSLA", shifted, now)
        self.assertEqual(s.status, "LATE_LONG")

    def test_weekend_closed(self):
        self.assertFalse(m.is_regular_session(datetime(2026, 8, 15, 10, 0, tzinfo=ET)))


if __name__ == "__main__":
    unittest.main()
