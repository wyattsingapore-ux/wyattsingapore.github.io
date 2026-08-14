#!/usr/bin/env python3
"""ChatGPT QCC trigger monitor.

Alert-only market monitor using Twelve Data 1-minute bars and Telegram.
No broker/order APIs are imported or called.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROJECT_ROOT = Path(__file__).resolve().parent
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
    candidates += [Path.cwd() / ".env", Path.cwd() / "qcc" / ".env"]
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
        req = Request(f"{self.BASE}/time_series?{params}", headers={"User-Agent": "qcc-chatgpt-trigger/1.0"})
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            raise MonitorError(f"Twelve Data HTTP {e.code}") from e
        except (URLError, TimeoutError, json.JSONDecodeError) as e:
            raise MonitorError(f"Twelve Data request failed: {type(e).__name__}") from e
        if payload.get("status") == "error" or "values" not in payload:
            raise MonitorError(f"Twelve Data error: {payload.get('message', 'invalid response')}")
        bars: List[Bar] = []
        for item in payload["values"]:
            try:
                dt = datetime.strptime(item["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
                bars.append(Bar(dt, float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"]), float(item.get("volume") or 0)))
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
        body = urlencode({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        req = Request(f"{self.BASE}/bot{self.token}/sendMessage", data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
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
    alpha = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def atr(bars: List[Bar], period: int = 14) -> float:
    trs = [max(cur.high-cur.low, abs(cur.high-prev.close), abs(cur.low-prev.close)) for prev, cur in zip(bars[:-1], bars[1:])]
    return sum(trs[-period:]) / period


def relative_volume(bars: List[Bar], baseline: int = 20) -> float:
    prior = [b.volume for b in bars[-(baseline + 1):-1] if b.volume > 0]
    if not prior:
        return 0.0
    mean = statistics.fmean(prior)
    return bars[-1].volume / mean if mean else 0.0


def split_current_and_completed(bars: List[Bar], now_et: datetime) -> Tuple[float, List[Bar]]:
    observed_price = bars[-1].close
    minute_floor = now_et.replace(second=0, microsecond=0)
    completed = [b for b in bars if b.dt < minute_floor]
    if len(completed) < 25:
        raise MonitorError("Insufficient completed bars")
    return observed_price, completed


def fixed_trigger(symbol: str, side: str) -> Optional[float]:
    for name in (f"TRIGGER_{symbol}_{side}", f"{symbol}_{side}"):
        raw = os.getenv(name)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return None


def derive_snapshot(symbol: str, bars: List[Bar], now_et: datetime) -> SignalSnapshot:
    observed, completed = split_current_and_completed(bars, now_et)
    confirm = completed[-1]
    lookback = env_int("CHATGPT_TRIGGER_LOOKBACK", 12)
    rv_period = env_int("CHATGPT_RVOL_BASELINE", 20)
    manual_up, manual_down = fixed_trigger(symbol, "UP"), fixed_trigger(symbol, "DOWN")
    structure = completed[-(lookback + 1):-1]
    if manual_up is not None or manual_down is not None:
        up = manual_up if manual_up is not None else max(b.high for b in structure)
        down = manual_down if manual_down is not None else min(b.low for b in structure)
        mode = "manual" if manual_up is not None and manual_down is not None else "hybrid"
    else:
        up, down, mode = max(b.high for b in structure), min(b.low for b in structure), "auto"
    rv = relative_volume(completed, rv_period)
    closes = [b.close for b in completed]
    e9, e21, a14 = ema(closes, 9), ema(closes, 21), atr(completed, 14)
    threshold = env_float("CHATGPT_RVOL_THRESHOLD", 1.5)
    status = "WAIT"
    if confirm.close > up and rv >= threshold:
        status = "LONG_CONFIRMED"
    elif confirm.close < down and rv >= threshold:
        status = "SHORT_CONFIRMED"
    else:
        pre = env_float("CHATGPT_PREALERT_PCT", 0.10) / 100.0
        if up > 0 and 0 <= (up-observed)/up <= pre:
            status = "NEAR_LONG"
        elif down > 0 and 0 <= (observed-down)/down <= pre:
            status = "NEAR_SHORT"
    return SignalSnapshot(symbol, mode, observed, confirm.dt.isoformat(), confirm.close, up, down, rv, e9, e21, a14, status)


def is_regular_session(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:
        return False
    return now_et.replace(hour=9, minute=30, second=0, microsecond=0) <= now_et < now_et.replace(hour=16, minute=0, second=0, microsecond=0)


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"symbols": {}, "last_check": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def should_send(s: SignalSnapshot, state: dict, now_et: datetime) -> bool:
    ss = state.setdefault("symbols", {}).setdefault(s.symbol, {})
    if s.status in ("LONG_CONFIRMED", "SHORT_CONFIRMED"):
        side = "LONG" if "LONG" in s.status else "SHORT"
        level = s.up_trigger if side == "LONG" else s.down_trigger
        key = f"{side}:{level:.4f}"
        if ss.get("last_confirmed") == key:
            return False
        ss["last_confirmed"], ss["last_confirmed_at"] = key, now_et.isoformat()
        return True
    if s.status in ("NEAR_LONG", "NEAR_SHORT"):
        side = "long" if s.status == "NEAR_LONG" else "short"
        k = f"pre_{side}_at"
        prior = ss.get(k)
        if prior:
            try:
                if now_et - datetime.fromisoformat(prior) < timedelta(minutes=env_int("CHATGPT_PREALERT_COOLDOWN_MIN", 10)):
                    return False
            except ValueError:
                pass
        ss[k] = now_et.isoformat()
        return True
    if s.down_trigger <= s.completed_close <= s.up_trigger:
        ss.pop("last_confirmed", None)
    return False


def format_alert(s: SignalSnapshot, now_et: datetime) -> str:
    if s.status == "LONG_CONFIRMED":
        heading, action = f"🔴 {s.symbol} LONG BREAKOUT CONFIRMED", "Open IBKR and inspect the appropriate same-day option chain."
    elif s.status == "SHORT_CONFIRMED":
        heading, action = f"🔴 {s.symbol} SHORT BREAKOUT CONFIRMED", "Open IBKR and inspect the appropriate same-day option chain."
    elif s.status == "NEAR_LONG":
        heading, action = f"🟡 {s.symbol} NEAR UPSIDE TRIGGER", "NO CONFIRMED SIGNAL YET"
    else:
        heading, action = f"🟡 {s.symbol} NEAR DOWNSIDE TRIGGER", "NO CONFIRMED SIGNAL YET"
    return (f"{heading}\n\nObserved: {s.observed_price:.2f}\n1m close: {s.completed_close:.2f}\nUp/Down: {s.up_trigger:.2f} / {s.down_trigger:.2f}\nRelative volume: {s.rel_volume:.2f}x\nEMA9/EMA21: {s.ema9:.2f} / {s.ema21:.2f}\nATR14: {s.atr14:.2f}\nMode: {s.mode}\nTime: {now_et.strftime('%H:%M:%S ET')}\n\n{action}")


def setup_logging(path: Path) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("chatgpt_trigger_monitor")
    log.setLevel(logging.INFO)
    if not log.handlers:
        for h in (logging.FileHandler(path), logging.StreamHandler(sys.stdout)):
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(h)
    return log


def symbols_from_env() -> List[str]:
    return [x.strip().upper() for x in os.getenv("CHATGPT_MONITOR_SYMBOLS", "TSLA,MSTR").split(",") if x.strip()]


def scan_once(client, telegram, state, logger, send_alerts=True, now_et=None):
    now_et = now_et or datetime.now(ET)
    snaps = []
    for symbol in symbols_from_env():
        try:
            snap = derive_snapshot(symbol, client.time_series(symbol, max(50, env_int("CHATGPT_OUTPUTSIZE", 50))), now_et)
            snaps.append(snap)
            logger.info("%s price=%.2f close=%.2f up=%.2f down=%.2f rv=%.2f status=%s mode=%s", symbol, snap.observed_price, snap.completed_close, snap.up_trigger, snap.down_trigger, snap.rel_volume, snap.status, snap.mode)
            if send_alerts and should_send(snap, state, now_et):
                telegram.send(format_alert(snap, now_et))
                logger.warning("ALERT %s %s", symbol, snap.status)
        except Exception as e:
            logger.error("%s scan failed: %s", symbol, str(e))
    state["last_check"] = now_et.isoformat()
    return snaps


def sleep_until_next_minute() -> None:
    now = datetime.now(ET)
    nxt = (now + timedelta(minutes=1)).replace(second=3, microsecond=0)
    time.sleep(max(1.0, (nxt-now).total_seconds()))


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-alerts", action="store_true")
    a = p.parse_args()
    state_path = Path(os.getenv("CHATGPT_STATE_FILE", str(DEFAULT_STATE)))
    log = setup_logging(Path(os.getenv("CHATGPT_LOG_FILE", str(DEFAULT_LOG))))
    if a.status:
        now = datetime.now(ET)
        state = load_state(state_path)
        print("ChatGPT Trigger Monitor")
        print(f"market: {'open' if is_regular_session(now) else 'closed'}")
        print(f"time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}")
        print(f"last check: {state.get('last_check') or 'never'}")
        print(f"symbols: {','.join(symbols_from_env())}")
        print(f"Twelve Data key: {'configured' if os.getenv('TWELVEDATA_API_KEY') else 'missing'}")
        print(f"Telegram: {'configured' if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID') else 'missing'}")
        return 0
    key = os.getenv("TWELVEDATA_API_KEY")
    if not key:
        print("TWELVEDATA_API_KEY is missing", file=sys.stderr)
        return 2
    tg = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    td = TwelveDataClient(key)
    if a.test_telegram:
        tg.send("🧪 TEST — ChatGPT QCC trigger monitor Telegram delivery is working. No trade signal.")
        print("Telegram test sent")
        return 0
    state = load_state(state_path)
    if a.once:
        now = datetime.now(ET)
        if not a.force and not is_regular_session(now):
            print("Market outside U.S. regular hours; use --force to test anyway.")
            return 0
        snaps = scan_once(td, tg, state, log, not a.no_alerts, now)
        save_state(state_path, state)
        for s in snaps:
            print(json.dumps(asdict(s), sort_keys=True))
        return 0 if snaps else 1
    log.info("monitor starting symbols=%s", ",".join(symbols_from_env()))
    while True:
        now = datetime.now(ET)
        if is_regular_session(now):
            scan_once(td, tg, state, log, True, now)
            save_state(state_path, state)
            sleep_until_next_minute()
        else:
            time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
