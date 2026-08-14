#!/usr/bin/env python3
"""Quota-aware QCC opportunity scanner.

Uses the existing ChatGPT 5-minute trigger engine but adds a quota-aware universe
scheduler:
  * full 14-symbol universe scan every 20 minutes
  * rank candidates
  * top candidate every minute
  * runner-up every 5 minutes

Alert-only. No broker/order APIs are imported or called.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import time
from typing import Dict, List

import chatgpt_trigger_monitor as core

ET = core.ET
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SELECTION = PROJECT_ROOT / "logs" / "chatgpt_opportunity_selection.json"
DEFAULT_UNIVERSE = "SPY,QQQ,TSLA,NVDA,MSTR,AVGO,AMD,META,AMZN,AAPL,GOOGL,MSFT,PLTR,COIN"


@dataclass
class RankedCandidate:
    symbol: str
    score: float
    bias: str
    reason: str
    snapshot: core.SignalSnapshot


def universe_from_env() -> List[str]:
    raw = os.getenv("CHATGPT_UNIVERSE_SYMBOLS", DEFAULT_UNIVERSE)
    out = []
    for item in raw.split(","):
        s = item.strip().upper()
        if s and s not in out:
            out.append(s)
    return out


def trading_telegram() -> core.TelegramClient:
    """Dedicated trading alert channel only; never fall back to Jarvis credentials."""
    return core.TelegramClient(
        os.getenv("QCC_TRADING_TELEGRAM_BOT_TOKEN"),
        os.getenv("QCC_TRADING_TELEGRAM_CHAT_ID"),
    )


def rank_snapshot(s: core.SignalSnapshot) -> RankedCandidate:
    """Rank opportunity quality from stock-only information."""
    atr = max(s.atr14, 1e-9)
    up_dist_atr = max(0.0, s.up_trigger - s.observed_price) / atr
    dn_dist_atr = max(0.0, s.observed_price - s.down_trigger) / atr
    if up_dist_atr <= dn_dist_atr:
        bias = "LONG"
        proximity = max(0.0, 1.5 - up_dist_atr) / 1.5
        trend = (s.ema9 - s.ema21) / atr
    else:
        bias = "SHORT"
        proximity = max(0.0, 1.5 - dn_dist_atr) / 1.5
        trend = (s.ema21 - s.ema9) / atr

    rv_component = min(max(s.rel_volume, 0.0), 3.0) / 3.0
    trend_component = min(max(trend, -1.0), 1.5)
    confirmed_bonus = 1.0 if s.status in ("LONG_CONFIRMED", "SHORT_CONFIRMED") else 0.0
    near_bonus = 0.35 if s.status in ("NEAR_LONG", "NEAR_SHORT") else 0.0
    late_penalty = 0.75 if s.status in ("LATE_LONG", "LATE_SHORT") else 0.0

    score = (
        45.0 * proximity
        + 25.0 * rv_component
        + 20.0 * max(0.0, trend_component) / 1.5
        + 20.0 * confirmed_bonus
        + 8.0 * near_bonus
        - 20.0 * late_penalty
    )
    score = max(0.0, min(100.0, score))
    reason = (
        f"{bias} proximity={proximity:.2f} rv={s.rel_volume:.2f}x "
        f"trendATR={trend:.2f} status={s.status}"
    )
    return RankedCandidate(s.symbol, round(score, 2), bias, reason, s)


def scan_symbols(symbols: List[str], client: core.TwelveDataClient,
                 telegram: core.TelegramClient, state: dict,
                 logger: logging.Logger, now_et: datetime,
                 send_alerts: bool = True) -> List[RankedCandidate]:
    ranked: List[RankedCandidate] = []
    for symbol in symbols:
        try:
            bars = client.time_series(symbol, outputsize=max(150, core.env_int("CHATGPT_OUTPUTSIZE", 150)))
            snap = core.derive_snapshot(symbol, bars, now_et)
            cand = rank_snapshot(snap)
            ranked.append(cand)
            logger.info(
                "SCAN %s score=%.2f bias=%s price=%.2f up=%.2f down=%.2f rv=%.2f status=%s",
                symbol, cand.score, cand.bias, snap.observed_price, snap.up_trigger,
                snap.down_trigger, snap.rel_volume, snap.status,
            )
            if send_alerts and core.should_send(snap, state, now_et):
                if telegram.configured:
                    telegram.send(core.format_alert(snap, now_et))
                    logger.warning("ALERT %s %s", symbol, snap.status)
                else:
                    logger.warning("ALERT SUPPRESSED %s %s dedicated trading Telegram not configured", symbol, snap.status)
        except Exception as e:
            logger.error("SCAN %s failed: %s", symbol, str(e))
    ranked.sort(key=lambda x: x.score, reverse=True)
    return ranked


def save_selection(path: Path, ranked: List[RankedCandidate], now_et: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": now_et.isoformat(),
        "ranked": [
            {
                "symbol": c.symbol,
                "score": c.score,
                "bias": c.bias,
                "reason": c.reason,
                "snapshot": asdict(c.snapshot),
            }
            for c in ranked
        ],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def load_selection(path: Path) -> List[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [x["symbol"] for x in data.get("ranked", []) if x.get("symbol")]
    except Exception:
        return []


def due_full_scan(now_et: datetime, state: dict) -> bool:
    every = max(10, core.env_int("CHATGPT_UNIVERSE_SCAN_MIN", 20))
    raw = state.get("last_universe_scan")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        return now_et - last >= timedelta(minutes=every)
    except Exception:
        return True


def due_runner_scan(now_et: datetime, state: dict) -> bool:
    every = max(2, core.env_int("CHATGPT_RUNNER_SCAN_MIN", 5))
    raw = state.get("last_runner_scan")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
        return now_et - last >= timedelta(minutes=every)
    except Exception:
        return True


def send_ranking_if_changed(ranked: List[RankedCandidate], telegram: core.TelegramClient,
                            state: dict, now_et: datetime, logger: logging.Logger) -> None:
    if not ranked or not telegram.configured:
        return
    top = ranked[:2]
    signature = ",".join(f"{c.symbol}:{c.bias}" for c in top)
    if state.get("ranking_signature") == signature:
        return
    state["ranking_signature"] = signature
    lines = ["📊 QCC TRADING WATCHLIST UPDATED", ""]
    for idx, c in enumerate(top, 1):
        lines.append(f"#{idx} {c.symbol} {c.bias} — score {c.score:.1f}")
        lines.append(f"   {c.reason}")
    lines += ["", f"Time: {now_et.strftime('%H:%M ET')}"]
    telegram.send("\n".join(lines))
    logger.warning("WATCHLIST %s", signature)


def print_ranked(ranked: List[RankedCandidate]) -> None:
    for c in ranked:
        print(json.dumps({
            "symbol": c.symbol,
            "score": c.score,
            "bias": c.bias,
            "reason": c.reason,
            "snapshot": asdict(c.snapshot),
        }, sort_keys=True))


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="scan the full universe once")
    p.add_argument("--force", action="store_true", help="allow --once outside regular hours")
    p.add_argument("--no-alerts", action="store_true")
    p.add_argument("--status", action="store_true")
    a = p.parse_args()

    state_path = Path(os.getenv("CHATGPT_STATE_FILE", str(core.DEFAULT_STATE)))
    log_path = Path(os.getenv("CHATGPT_LOG_FILE", str(core.DEFAULT_LOG)))
    selection_path = Path(os.getenv("CHATGPT_SELECTION_FILE", str(DEFAULT_SELECTION)))
    logger = core.setup_logging(log_path)
    state = core.load_state(state_path)

    if a.status:
        print(core.status_report(state_path))
        print(f"universe: {','.join(universe_from_env())}")
        print(f"selected: {','.join(load_selection(selection_path)[:2]) or 'none'}")
        print(f"universe cadence: {core.env_int('CHATGPT_UNIVERSE_SCAN_MIN', 20)}m")
        print(f"runner cadence: {core.env_int('CHATGPT_RUNNER_SCAN_MIN', 5)}m")
        tg = trading_telegram()
        print(f"dedicated trading Telegram: {'configured' if tg.configured else 'missing'}")
        return 0

    api_key = os.getenv("TWELVEDATA_API_KEY")
    if not api_key:
        raise SystemExit("TWELVEDATA_API_KEY is missing")
    client = core.TwelveDataClient(api_key)
    telegram = trading_telegram()

    if a.once:
        now = datetime.now(ET)
        if not a.force and not core.is_regular_session(now):
            print("Market is outside U.S. regular hours; use --force to test anyway.")
            return 0
        ranked = scan_symbols(universe_from_env(), client, telegram, state, logger, now, not a.no_alerts)
        save_selection(selection_path, ranked, now)
        state["last_universe_scan"] = now.isoformat()
        core.save_state(state_path, state)
        print_ranked(ranked)
        return 0 if ranked else 1

    logger.info("opportunity scanner starting universe=%s", ",".join(universe_from_env()))
    while True:
        now = datetime.now(ET)
        if not core.is_regular_session(now):
            time.sleep(30)
            continue

        scanned: Dict[str, RankedCandidate] = {}
        if due_full_scan(now, state):
            ranked = scan_symbols(universe_from_env(), client, telegram, state, logger, now, True)
            for c in ranked:
                scanned[c.symbol] = c
            save_selection(selection_path, ranked, now)
            send_ranking_if_changed(ranked, telegram, state, now, logger)
            state["last_universe_scan"] = now.isoformat()
        else:
            selected = load_selection(selection_path)
            if not selected:
                selected = universe_from_env()[:2]
            top = selected[0:1]
            if top:
                for c in scan_symbols(top, client, telegram, state, logger, now, True):
                    scanned[c.symbol] = c
            if len(selected) > 1 and due_runner_scan(now, state):
                for c in scan_symbols(selected[1:2], client, telegram, state, logger, now, True):
                    scanned[c.symbol] = c
                state["last_runner_scan"] = now.isoformat()

        state["last_check"] = now.isoformat()
        core.save_state(state_path, state)
        core.sleep_until_next_minute()


if __name__ == "__main__":
    raise SystemExit(main())
