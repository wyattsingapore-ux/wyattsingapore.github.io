#!/usr/bin/env python3
"""ChatGPT QCC trigger monitor.

Alert-only market monitor using Twelve Data 1-minute bars and Telegram.
No broker/order APIs are imported or called.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = PROJECT_ROOT / "logs" / "chatgpt_trigger_state.json"
DEFAULT_LOG = PROJECT_ROOT / "logs" / "chatgpt_trigger_monitor.log"


def load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value and value[0:1] == value[-1:] and value[0] in "'\"":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def load_known_envs() -> None:
    candidates = []
    explicit = os.getenv("QCC_ENV_FILE")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [PROJECT_ROOT / ".env", PROJECT_ROOT / "qcc" / ".env"]
    for p in candidates:
        load_env_file(p)


load_known_envs()


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Bar:
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SignalSnapshot:
    symbol: str
    mode: str
    observed_price: float
    completed_dt: str
    completed_close: float
    up_trigger: float
    down_trigger: float
    rel_volume: float
    ema9: float
    ema21: float
    atr14: float
    status: str


class MonitorError(RuntimeError):
    pass


class TwelveDataClient:
    BASE = "https://api.twelvedata.com"

    def __init__(self, api_key: str, timeout: int = 12):
        self.api_key = api_key
        self.timeout = timeout

    def time_series(self, symbol: str, outputsize: int = 50) -> List[Bar]:
        params = urlencode({
            "symbol": symbol,
            "interval": "1min",
            "outputsize": outputsize,
            "timezone": "America/New_York",
            "apikey": self.api_key,
        })
        url = f"{self.BASE}/time_series?{params}"
        req = Request(url, headers={"User-Agent": "qcc-chatgpt-trigger/1.0"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            raise MonitorError(f"Twelve Data HTTP {e.code}") from e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            raise MonitorError(f"Twelve Data request failed: {type(e).__name__}") from e

        if payload.get("status") == "error" or "values" not in payload:
            msg = payload.get("message", "invalid response")
            raise MonitorError(f"Twelve Data error: {msg}")

        bars: List[Bar] = []
        for item in payload["values"]:
            try:
                dt = datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                bars.append(Bar(
                    dt=dt,
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume") or 0),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        if len(bars) < 25:
            raise MonitorError(f"Insufficient 1m bars for {symbol}: {len(bars)}")
        bars.sort(key=lambda b: b.dt)
        return bars


class TelegramClient:
    BASE = "https://api.telegram.org"

    def __init__(self, token: Optional[str], chat_id: Optional[str], timeout: int = 12):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.configured:
            raise MonitorError("Telegram not configured")
        url = f"{self.BASE}/bot{self.token}/sendMessage"
        body = urlencode({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if not payload.get("ok"):
                raise MonitorError("Telegram send failed")
        except HTTPError as e:
            raise MonitorError(f"Telegram HTTP {e.code}") from e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            raise MonitorError(f"Telegram request failed: {type(e).__name__}") from e


def ema(values: List[float], period: int) -> float:
    if len(values) < period:
        raise MonitorError(f"Need {period} values for EMA")
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def atr(bars: List[Bar], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise MonitorError("Insufficient bars for ATR")
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs[-period:]) / period


def relative_volume(bars: List[Bar], baseline: int = 20) -> float:
    if len(bars) < baseline + 1:
        raise MonitorError("Insufficient bars for relative volume")
    current = bars[-1].volume
    prior = [b.volume for b in bars[-(baseline + 1):-1] if b.volume > 0]
    if not prior:
        return 0.0
    mean = statistics.fmean(prior)
    return current / mean if mean else 0.0


def split_current_and_completed(bars: List[Bar], now_et: datetime) -> Tuple[float, List[Bar]]:
    """Return observed latest price and conservatively finalized 1-minute bars.

    Twelve Data documents that REST candles can take up to roughly two minutes
    after close to become final. We therefore exclude bars whose *end time* is
    newer than CHATGPT_FINALIZATION_LAG_SEC (default 120s).
    """
    if not bars:
        raise MonitorError("No bars")
    observed_price = bars[-1].close
    lag = env_int("CHATGPT_FINALIZATION_LAG_SEC", 120)
    cutoff = now_et - timedelta(seconds=lag)
    completed = [b for b in bars if b.dt + timedelta(minutes=1) <= cutoff]
    if len(completed) < 25:
        raise MonitorError("Insufficient finalized 1m bars")
    return observed_price, completed


def aggregate_5m(bars: List[Bar]) -> List[Bar]:
    """Aggregate finalized 1-minute bars into complete, aligned 5-minute bars.

    Only buckets containing all five contiguous one-minute bars are emitted.
    This gives us Claude-style 5-minute confirmation without another API call.
    """
    buckets = {}
    for b in bars:
        minute = (b.dt.minute // 5) * 5
        key = b.dt.replace(minute=minute, second=0, microsecond=0)
        buckets.setdefault(key, []).append(b)
    out: List[Bar] = []
    for key in sorted(buckets):
        group = sorted(buckets[key], key=lambda x: x.dt)
        if len(group) != 5:
            continue
        expected = [key + timedelta(minutes=i) for i in range(5)]
        if [b.dt for b in group] != expected:
            continue
        out.append(Bar(
            key, group[0].open, max(b.high for b in group),
            min(b.low for b in group), group[-1].close,
            sum(b.volume for b in group),
        ))
    return out


def entry_cutoff_passed(now_et: datetime) -> bool:
    raw = os.getenv("CHATGPT_ENTRY_CUTOFF_ET", "15:30")
    try:
        hh, mm = (int(x) for x in raw.split(":", 1))
    except Exception:
        hh, mm = 15, 30
    cutoff = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now_et >= cutoff


def fixed_trigger(symbol: str, side: str) -> Optional[float]:
    for name in (f"TRIGGER_{symbol}_{side}", f"{symbol}_{side}"):
        raw = os.getenv(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return None


def derive_snapshot(symbol: str, bars: List[Bar], now_et: datetime) -> SignalSnapshot:
    observed, finalized_1m = split_current_and_completed(bars, now_et)
    completed = aggregate_5m(finalized_1m)
    if len(completed) < 22:
        raise MonitorError("Insufficient complete 5m bars for indicators")
    confirm = completed[-1]
    lookback = env_int("CHATGPT_TRIGGER_LOOKBACK", 12)
    rv_period = env_int("CHATGPT_RVOL_BASELINE", 20)
    if len(completed) < max(22, lookback + 2, rv_period + 2):
        raise MonitorError("Insufficient complete 5m bars for indicators")

    manual_up = fixed_trigger(symbol, "UP")
    manual_down = fixed_trigger(symbol, "DOWN")
    structure = completed[-(lookback + 1):-1]
    if manual_up is not None or manual_down is not None:
        up = manual_up if manual_up is not None else max(b.high for b in structure)
        down = manual_down if manual_down is not None else min(b.low for b in structure)
        mode = "manual-5m" if manual_up is not None and manual_down is not None else "hybrid-5m"
    else:
        up, down, mode = max(b.high for b in structure), min(b.low for b in structure), "auto-5m"

    rv = relative_volume(completed, rv_period)
    closes = [b.close for b in completed]
    e9, e21, a14 = ema(closes, 9), ema(closes, 21), atr(completed, 14)
    rv_threshold = env_float("CHATGPT_RVOL_THRESHOLD", 1.5)

    status = "WAIT"
    long_break = confirm.close > up and rv >= rv_threshold
    short_break = confirm.close < down and rv >= rv_threshold
    if long_break:
        status = "LATE_LONG" if entry_cutoff_passed(now_et) else "LONG_CONFIRMED"
    elif short_break:
        status = "LATE_SHORT" if entry_cutoff_passed(now_et) else "SHORT_CONFIRMED"
    else:
        pre_pct = env_float("CHATGPT_PREALERT_PCT", 0.10) / 100.0
        if up > 0 and 0 <= (up - observed) / up <= pre_pct:
            status = "NEAR_LONG"
        elif down > 0 and 0 <= (observed - down) / down <= pre_pct:
            status = "NEAR_SHORT"

    return SignalSnapshot(
        symbol=symbol, mode=mode, observed_price=observed,
        completed_dt=confirm.dt.isoformat(), completed_close=confirm.close,
        up_trigger=up, down_trigger=down, rel_volume=rv,
        ema9=e9, ema21=e21, atr14=a14, status=status,
    )


def is_regular_session(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now_et < close_t


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"symbols": {}, "last_check": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def trigger_key(s: SignalSnapshot) -> str:
    side = "LONG" if "LONG" in s.status else "SHORT"
    level = s.up_trigger if side == "LONG" else s.down_trigger
    return f"{side}:{level:.4f}"


def should_send(snapshot: SignalSnapshot, state: dict, now_et: datetime) -> bool:
    symstate = state.setdefault("symbols", {}).setdefault(snapshot.symbol, {})
    if snapshot.status in ("LONG_CONFIRMED", "SHORT_CONFIRMED", "LATE_LONG", "LATE_SHORT"):
        key = trigger_key(snapshot)
        if symstate.get("last_confirmed") == key:
            return False
        symstate["last_confirmed"] = key
        symstate["last_confirmed_at"] = now_et.isoformat()
        return True

    if snapshot.status in ("NEAR_LONG", "NEAR_SHORT"):
        side = "LONG" if snapshot.status == "NEAR_LONG" else "SHORT"
        k = f"pre_{side.lower()}_at"
        cooldown = env_int("CHATGPT_PREALERT_COOLDOWN_MIN", 10)
        prior = symstate.get(k)
        if prior:
            try:
                dt = datetime.fromisoformat(prior)
                if now_et - dt < timedelta(minutes=cooldown):
                    return False
            except ValueError:
                pass
        symstate[k] = now_et.isoformat()
        return True

    if snapshot.down_trigger <= snapshot.completed_close <= snapshot.up_trigger:
        symstate.pop("last_confirmed", None)
    return False


def format_alert(s: SignalSnapshot, now_et: datetime) -> str:
    if s.status == "LONG_CONFIRMED":
        heading = f"🔴 {s.symbol} LONG BREAKOUT CONFIRMED"
        action = "Open IBKR and inspect the appropriate same-day option chain."
    elif s.status == "SHORT_CONFIRMED":
        heading = f"🔴 {s.symbol} SHORT BREAKOUT CONFIRMED"
        action = "Open IBKR and inspect the appropriate same-day option chain."
    elif s.status == "LATE_LONG":
        heading = f"🟠 {s.symbol} LATE LONG BREAKOUT"
        action = "After the 15:30 ET entry cutoff — informational only; do not treat as a new 0DTE entry signal."
    elif s.status == "LATE_SHORT":
        heading = f"🟠 {s.symbol} LATE SHORT BREAKOUT"
        action = "After the 15:30 ET entry cutoff — informational only; do not treat as a new 0DTE entry signal."
    elif s.status == "NEAR_LONG":
        heading = f"🟡 {s.symbol} NEAR UPSIDE TRIGGER"
        action = "NO CONFIRMED SIGNAL YET"
    else:
        heading = f"🟡 {s.symbol} NEAR DOWNSIDE TRIGGER"
        action = "NO CONFIRMED SIGNAL YET"

    return (
        f"{heading}\n\n"
        f"Observed: {s.observed_price:.2f}\n"
        f"5m close: {s.completed_close:.2f}\n"
        f"Up/Down: {s.up_trigger:.2f} / {s.down_trigger:.2f}\n"
        f"Relative volume: {s.rel_volume:.2f}x\n"
        f"EMA9/EMA21: {s.ema9:.2f} / {s.ema21:.2f}\n"
        f"ATR14: {s.atr14:.2f}\n"
        f"Mode: {s.mode}\n"
        f"Time: {now_et.strftime('%H:%M:%S ET')}\n\n"
        f"{action}"
    )


def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("chatgpt_trigger_monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(fh)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(sh)
    return logger


def symbols_from_env() -> List[str]:
    raw = os.getenv("CHATGPT_MONITOR_SYMBOLS", "TSLA,MSTR")
    return [x.strip().upper() for x in raw.split(",") if x.strip()]


def scan_once(client: TwelveDataClient, telegram: TelegramClient, state: dict,
              logger: logging.Logger, send_alerts: bool = True,
              now_et: Optional[datetime] = None) -> List[SignalSnapshot]:
    now_et = now_et or datetime.now(ET)
    snapshots: List[SignalSnapshot] = []
    for symbol in symbols_from_env():
        try:
            bars = client.time_series(symbol, outputsize=max(150, env_int("CHATGPT_OUTPUTSIZE", 150)))
            snap = derive_snapshot(symbol, bars, now_et)
            snapshots.append(snap)
            logger.info(
                "%s price=%.2f close=%.2f up=%.2f down=%.2f rv=%.2f status=%s mode=%s",
                symbol, snap.observed_price, snap.completed_close, snap.up_trigger,
                snap.down_trigger, snap.rel_volume, snap.status, snap.mode,
            )
            if send_alerts and should_send(snap, state, now_et):
                telegram.send(format_alert(snap, now_et))
                logger.warning("ALERT %s %s", symbol, snap.status)
        except Exception as e:
            logger.error("%s scan failed: %s", symbol, str(e))
    state["last_check"] = now_et.isoformat()
    return snapshots


def status_report(state_path: Path) -> str:
    state = load_state(state_path)
    now = datetime.now(ET)
    td = bool(os.getenv("TWELVEDATA_API_KEY"))
    tg = bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))
    lines = [
        "ChatGPT Trigger Monitor",
        f"market: {'open' if is_regular_session(now) else 'closed'}",
        f"time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}",
        f"last check: {state.get('last_check') or 'never'}",
        f"symbols: {','.join(symbols_from_env())}",
        f"Twelve Data key: {'configured' if td else 'missing'}",
        f"Telegram: {'configured' if tg else 'missing'}",
    ]
    return "\n".join(lines)


def sleep_until_next_minute() -> None:
    now = datetime.now(ET)
    nxt = (now + timedelta(minutes=1)).replace(second=3, microsecond=0)
    time.sleep(max(1.0, (nxt - now).total_seconds()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one market scan")
    parser.add_argument("--status", action="store_true", help="show local configuration/status")
    parser.add_argument("--test-telegram", action="store_true", help="send one clearly marked Telegram test")
    parser.add_argument("--force", action="store_true", help="allow --once outside RTH")
    parser.add_argument("--no-alerts", action="store_true", help="scan/log without sending alerts")
    args = parser.parse_args()

    state_path = Path(os.getenv("CHATGPT_STATE_FILE", str(DEFAULT_STATE)))
    log_path = Path(os.getenv("CHATGPT_LOG_FILE", str(DEFAULT_LOG)))
    logger = setup_logging(log_path)

    if args.status:
        print(status_report(state_path))
        return 0

    api_key = os.getenv("TWELVEDATA_API_KEY")
    if not api_key:
        print("TWELVEDATA_API_KEY is missing", file=sys.stderr)
        return 2
    telegram = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    client = TwelveDataClient(api_key)

    if args.test_telegram:
        telegram.send("🧪 TEST — ChatGPT QCC trigger monitor Telegram delivery is working. No trade signal.")
        print("Telegram test sent")
        return 0

    state = load_state(state_path)

    if args.once:
        now = datetime.now(ET)
        if not args.force and not is_regular_session(now):
            print("Market is outside U.S. regular hours; use --force to test anyway.")
            return 0
        snaps = scan_once(client, telegram, state, logger, send_alerts=not args.no_alerts, now_et=now)
        save_state(state_path, state)
        for s in snaps:
            print(json.dumps(asdict(s), sort_keys=True))
        return 0 if snaps else 1

    logger.info("monitor starting symbols=%s", ",".join(symbols_from_env()))
    while True:
        now = datetime.now(ET)
        if is_regular_session(now):
            scan_once(client, telegram, state, logger, send_alerts=True, now_et=now)
            save_state(state_path, state)
            sleep_until_next_minute()
        else:
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
