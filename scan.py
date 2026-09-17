"""
scan.py — the actual engine. Run this on a schedule (GitHub Actions later;
your own machine for now) and it will:

  1. Scan the full asset universe on 1H/4H/1D/1W for a fresh RSI>70 or
     RSI<30 extreme ("WATCHING").
  2. For anything WATCHING (this run or a prior one, within the visibility
     window), drop to the mapped lower timeframe(s) and run the
     WATCHING -> PULLBACK -> CONVERGING -> TRIGGERED state machine against
     LSMA(50,3) + MACD, per your rules.
  3. Persist state between runs (state.json) so an asset doesn't lose its
     progress just because you re-run the script.
  4. Write data.json for the dashboard.
  5. Fire a batched Discord alert for anything new this run (new WATCHING
     adds, and — the important one — fresh TRIGGERED entries with a
     buy-stop/sell-stop price attached).

Local testing: `python scan.py` with no DISCORD_WEBHOOK_URL set will just
log what it would have sent, instead of failing.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

import config
import fetch
import indicators

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scan")


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def build_universe() -> list[dict]:
    """Every (asset_class, ticker, display_name) triple we scan."""
    universe = []

    for display_name, ticker in config.FOREX_PAIRS.items():
        universe.append({"asset_class": "forex", "ticker": ticker, "display_name": display_name})

    for display_name, ticker in config.METALS.items():
        universe.append({"asset_class": "metals", "ticker": ticker, "display_name": display_name})

    for symbol in fetch.get_kraken_usd_pairs():
        universe.append({"asset_class": "crypto", "ticker": symbol, "display_name": symbol})

    logger.info("Universe built: %d assets", len(universe))
    return universe


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    path = Path(config.STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        logger.error("state.json is corrupt — starting fresh. Back up the old file if you need it.")
        return {}


def save_state(state: dict) -> None:
    Path(config.STATE_FILE).write_text(json.dumps(state, indent=2, default=str))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize_candles(df: pd.DataFrame, n: int = config.DASHBOARD_CANDLE_WINDOW) -> list:
    """
    Trailing window of OHLC + indicator values for dashboard.html's charts.
    `time` is Unix seconds (UTC) — lightweight-charts' native format.
    """
    candles = []
    for ts, row in df.tail(n).iterrows():
        ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        candles.append({
            "time": int(ts_utc.timestamp()),
            "open": round(float(row["open"]), 6),
            "high": round(float(row["high"]), 6),
            "low": round(float(row["low"]), 6),
            "close": round(float(row["close"]), 6),
            "ema10": None if pd.isna(row["ema10"]) else round(float(row["ema10"]), 6),
            "ema20": None if pd.isna(row["ema20"]) else round(float(row["ema20"]), 6),
            "lsma": None if pd.isna(row["lsma"]) else round(float(row["lsma"]), 6),
            "macd": None if pd.isna(row["macd"]) else round(float(row["macd"]), 6),
            "macd_signal": None if pd.isna(row["macd_signal"]) else round(float(row["macd_signal"]), 6),
            "macd_hist": None if pd.isna(row["macd_hist"]) else round(float(row["macd_hist"]), 6),
        })
    return candles


# ---------------------------------------------------------------------------
# Phase A — higher-timeframe RSI scan
# ---------------------------------------------------------------------------

def scan_higher_timeframe(asset: dict, htf: str, state: dict, watch_events: list) -> None:
    """
    Fetch + check one asset on one higher timeframe. Mutates `state` in
    place: creates a new WATCHING entry on a fresh RSI trigger, or advances
    the candle counter on an existing entry so its visibility window ticks
    forward every time a new higher-TF bar actually closes.
    """
    key = f"{asset['asset_class']}:{asset['ticker']}:{htf}"

    df = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], htf)
    if df is None or len(df) < config.MIN_WARMUP_BARS:
        if key in state:
            logger.warning("Fetch too short/failed for tracked entry %s — leaving state untouched this run", key)
        return

    df = indicators.compute_all(df)
    latest = df.iloc[-1]
    latest_bar_time = str(df.index[-1])
    rsi = latest["rsi"]

    entry = state.get(key)

    # Advance the visibility-window counter exactly once per new closed bar,
    # regardless of whether RSI is still outside the band.
    if entry is not None:
        if entry.get("last_htf_bar_time") != latest_bar_time:
            entry["htf_candle_count"] = entry.get("htf_candle_count", 0) + 1
            entry["last_htf_bar_time"] = latest_bar_time

    is_overbought = rsi > config.RSI_OVERBOUGHT
    is_oversold = rsi < config.RSI_OVERSOLD

    if entry is None and (is_overbought or is_oversold):
        direction = config.DIRECTION_BULLISH if is_overbought else config.DIRECTION_BEARISH
        entry = {
            "asset_class": asset["asset_class"],
            "ticker": asset["ticker"],
            "display_name": asset["display_name"],
            "higher_tf": htf,
            "direction": direction,
            "rsi_at_trigger": round(float(rsi), 2),
            "first_seen": now_iso(),
            "htf_candle_count": 0,
            "last_htf_bar_time": latest_bar_time,
            "lower_tf_states": {
                ltf: {"state": config.STATE_WATCHING, "updated_at": now_iso()}
                for ltf in config.LOWER_TF_MAP[htf]
            },
        }
        state[key] = entry
        watch_events.append(entry)
        logger.info("NEW WATCH: %s %s RSI=%.1f (%s)", asset["display_name"], htf, rsi, direction)

    if entry is not None:
        entry["last_rsi"] = round(float(rsi), 2)
        entry["candles"] = serialize_candles(df)


def expire_stale_entries(state: dict) -> None:
    """Drop anything past its visibility window. Runs after every scan pass."""
    expired = [
        key for key, entry in state.items()
        if entry.get("htf_candle_count", 0) > config.VISIBILITY_WINDOW_CANDLES
    ]
    for key in expired:
        logger.info("EXPIRED (past %d-candle window): %s", config.VISIBILITY_WINDOW_CANDLES, key)
        del state[key]


# ---------------------------------------------------------------------------
# Phase B — lower-timeframe state machine
# ---------------------------------------------------------------------------

def update_lower_tf_state(entry: dict, ltf: str, trigger_events: list) -> None:
    """
    Runs the WATCHING -> PULLBACK -> CONVERGING -> TRIGGERED machine for one
    (asset, lower_tf) pair, using the latest closed candle.
    """
    df = fetch.fetch_ohlc(entry["asset_class"], entry["ticker"], ltf)
    if df is None or len(df) < config.MIN_WARMUP_BARS:
        logger.warning("Lower-TF fetch too short/failed: %s / %s — skipping this cycle", entry["display_name"], ltf)
        return

    df = indicators.compute_all(df)
    latest = df.iloc[-1]

    price = latest["close"]
    lsma = latest["lsma"]
    macd_hist = latest["macd_hist"]
    macd = latest["macd"]
    macd_signal = latest["macd_signal"]

    if pd.isna(lsma) or pd.isna(macd):
        return  # indicators not warmed up yet for this slice — try again next run

    direction = entry["direction"]
    ltf_state = entry["lower_tf_states"].setdefault(
        ltf, {"state": config.STATE_WATCHING, "updated_at": now_iso()}
    )
    prev_state = ltf_state["state"]

    # "Wrong side" of LSMA depends on direction: bullish continuation wants
    # price pulling back BELOW the line before reclaiming it; bearish wants
    # the mirror (pulling back ABOVE before breaking back below).
    if direction == config.DIRECTION_BULLISH:
        on_wrong_side = price < lsma
        macd_confirmed = macd_hist > 0
    else:
        on_wrong_side = price > lsma
        macd_confirmed = macd_hist < 0

    macd_gap_pct = abs(macd - macd_signal) / abs(macd) if macd != 0 else float("inf")
    macd_close = macd_gap_pct <= config.MACD_CLOSENESS_PCT

    if prev_state == config.STATE_TRIGGERED:
        # Sticky once triggered — the stop level already fired, we just keep
        # showing it for the rest of the visibility window.
        new_state = config.STATE_TRIGGERED
    elif on_wrong_side:
        new_state = config.STATE_CONVERGING if macd_close else config.STATE_PULLBACK
    else:
        # Price is on the "right" side of LSMA this candle. If we were
        # already in a pullback and MACD backs the direction (or is close
        # enough to it), this candle IS the entry trigger.
        if prev_state in (config.STATE_PULLBACK, config.STATE_CONVERGING) and (macd_confirmed or macd_close):
            new_state = config.STATE_TRIGGERED
        else:
            new_state = config.STATE_WATCHING

    ltf_state["state"] = new_state
    ltf_state["updated_at"] = now_iso()
    ltf_state["price"] = round(float(price), 6)
    ltf_state["lsma"] = round(float(lsma), 6)
    ltf_state["macd_gap_pct"] = round(float(macd_gap_pct), 4) if macd_gap_pct != float("inf") else None
    ltf_state["candles"] = serialize_candles(df)

    fresh_trigger = new_state == config.STATE_TRIGGERED and prev_state != config.STATE_TRIGGERED
    if fresh_trigger:
        stop_price = float(latest["high"]) if direction == config.DIRECTION_BULLISH else float(latest["low"])
        ltf_state["stop_price"] = round(stop_price, 6)
        ltf_state["stop_type"] = "BUY_STOP" if direction == config.DIRECTION_BULLISH else "SELL_STOP"
        trigger_events.append({
            "display_name": entry["display_name"],
            "asset_class": entry["asset_class"],
            "higher_tf": entry["higher_tf"],
            "entry_tf": ltf,
            "direction": direction,
            "stop_type": ltf_state["stop_type"],
            "stop_price": ltf_state["stop_price"],
        })
        logger.info(
            "TRIGGERED: %s (%s trigger) entry@%s -> %s @ %.6f",
            entry["display_name"], entry["higher_tf"], ltf, ltf_state["stop_type"], stop_price,
        )


# ---------------------------------------------------------------------------
# Discord alerts
# ---------------------------------------------------------------------------

def send_discord_alert(watch_events: list, trigger_events: list) -> None:
    webhook_url = os.environ.get(config.DISCORD_WEBHOOK_ENV)
    if not watch_events and not trigger_events:
        logger.info("Nothing new this run — no Discord alert sent.")
        return
    if not webhook_url:
        logger.info(
            "%s not set — skipping Discord send. Would have alerted: %d new watches, %d new triggers.",
            config.DISCORD_WEBHOOK_ENV, len(watch_events), len(trigger_events),
        )
        return

    embeds = []

    if trigger_events:
        embeds.append({
            "title": "🎯 Entry Triggered",
            "color": 3066993,  # green
            "fields": [
                {
                    "name": f"{e['display_name']} — {e['stop_type']}",
                    "value": f"{e['higher_tf']} trigger → {e['entry_tf']} entry @ {e['stop_price']}",
                    "inline": False,
                }
                for e in trigger_events
            ],
        })

    if watch_events:
        embeds.append({
            "title": "👀 New on Watchlist",
            "color": 15105570,  # amber
            "fields": [
                {
                    "name": f"{e['display_name']} ({e['higher_tf']})",
                    "value": f"{e['direction']} bias — RSI {e['rsi_at_trigger']}",
                    "inline": True,
                }
                for e in watch_events
            ],
        })

    try:
        resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
        resp.raise_for_status()
        logger.info("Discord alert sent: %d triggers, %d new watches.", len(trigger_events), len(watch_events))
    except Exception as exc:  # noqa: BLE001
        logger.error("Discord send failed: %s", exc)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(state: dict) -> None:
    Path(config.OUTPUT_FILE).write_text(json.dumps({
        "generated_at": now_iso(),
        "visibility_window_candles": config.VISIBILITY_WINDOW_CANDLES,
        "assets": list(state.values()),
    }, indent=2, default=str))
    logger.info("Wrote %s with %d active entries.", config.OUTPUT_FILE, len(state))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    state = load_state()
    universe = build_universe()

    watch_events: list = []
    trigger_events: list = []

    logger.info("Phase A: higher-timeframe RSI scan (%s)", ", ".join(config.HIGHER_TIMEFRAMES))
    for asset in universe:
        for htf in config.HIGHER_TIMEFRAMES:
            scan_higher_timeframe(asset, htf, state, watch_events)

    expire_stale_entries(state)

    logger.info("Phase B: lower-timeframe state machine for %d active entries", len(state))
    for entry in state.values():
        for ltf in config.LOWER_TF_MAP[entry["higher_tf"]]:
            update_lower_tf_state(entry, ltf, trigger_events)

    write_output(state)
    save_state(state)
    send_discord_alert(watch_events, trigger_events)


if __name__ == "__main__":
    run()
