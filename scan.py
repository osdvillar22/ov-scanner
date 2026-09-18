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

import backtest
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

    for display_name, ticker in config.INDICES.items():
        universe.append({"asset_class": "indices", "ticker": ticker, "display_name": display_name})

    for display_name, ticker in config.ENERGY.items():
        universe.append({"asset_class": "energy", "ticker": ticker, "display_name": display_name})

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


def to_utc_iso(ts: pd.Timestamp) -> str:
    """A candle's own timestamp, UTC-normalized — same convention
    serialize_candles uses, so a marker built from this lines up exactly
    with that candle's `time` on the dashboard's chart."""
    ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts_utc.isoformat()


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
    new_bar_closed = False
    if entry is not None:
        if entry.get("last_htf_bar_time") != latest_bar_time:
            entry["htf_candle_count"] = entry.get("htf_candle_count", 0) + 1
            entry["last_htf_bar_time"] = latest_bar_time
            new_bar_closed = True

    is_overbought = rsi > config.RSI_OVERBOUGHT
    is_oversold = rsi < config.RSI_OVERSOLD

    if entry is not None and new_bar_closed and (is_overbought or is_oversold):
        # A fresh RSI extreme just printed on an already-watchlisted asset —
        # re-anchor the entry setup to THIS candle instead of the stale one
        # that first triggered the watch (same rule as htf_bias_series).
        # Any pullback/breakout progress tied to the old anchor no longer
        # applies, so every lower-tf tracker resets to WATCHING.
        new_direction = config.DIRECTION_BULLISH if is_overbought else config.DIRECTION_BEARISH
        entry["direction"] = new_direction
        entry["rsi_at_trigger"] = round(float(rsi), 2)
        entry["htf_candle_count"] = 0
        entry["htf_trigger_time"] = df.index[-1].isoformat()
        for ltf_state in entry["lower_tf_states"].values():
            ltf_state.clear()
            ltf_state.update({"state": config.STATE_WATCHING, "updated_at": now_iso()})
        logger.info(
            "RE-ANCHORED: %s %s RSI=%.1f (%s) — fresh extreme, entry-setup tracking reset",
            asset["display_name"], htf, rsi, new_direction,
        )

    if entry is None and (is_overbought or is_oversold):
        direction = config.DIRECTION_BULLISH if is_overbought else config.DIRECTION_BEARISH

        # Don't cold-start at "just triggered now" — replay the history we
        # already fetched to find the *real* trigger bar. Without this, an
        # asset whose RSI crossed 70 a week ago (before this scanner ever
        # ran, or before this specific entry existed) would show up as if
        # it just triggered this instant, with its lower-tf tracking blind
        # to everything that already happened since the real trigger.
        origin = backtest.htf_trigger_origin(df)
        if origin is not None and origin["direction"] == direction:
            htf_candle_count = origin["htf_candle_count"]
            rsi_at_trigger = origin["rsi_at_trigger"]
            trigger_time_iso = origin["trigger_time"].isoformat()
        else:
            htf_candle_count = 0
            rsi_at_trigger = round(float(rsi), 2)
            trigger_time_iso = None

        entry = {
            "asset_class": asset["asset_class"],
            "ticker": asset["ticker"],
            "display_name": asset["display_name"],
            "higher_tf": htf,
            "direction": direction,
            "rsi_at_trigger": rsi_at_trigger,
            "first_seen": trigger_time_iso or now_iso(),
            "htf_candle_count": htf_candle_count,
            "last_htf_bar_time": latest_bar_time,
            "htf_trigger_time": trigger_time_iso,
            "lower_tf_states": {
                ltf: {"state": config.STATE_WATCHING, "updated_at": now_iso(), "needs_backfill": True}
                for ltf in config.LOWER_TF_MAP[htf]
            },
        }
        state[key] = entry
        watch_events.append(entry)
        logger.info(
            "NEW WATCH: %s %s RSI=%.1f (%s) [real trigger %s, %d candles ago]",
            asset["display_name"], htf, rsi, direction, trigger_time_iso, htf_candle_count,
        )

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
    ltf_state = entry["lower_tf_states"].setdefault(
        ltf, {"state": config.STATE_WATCHING, "updated_at": now_iso()}
    )

    # First time we're touching this lower-tf state: replay the history we
    # already fetched (from the real higher-tf trigger bar onward) instead
    # of cold-starting at WATCHING, blind to a pullback/entry that may have
    # already fully played out before this entry existed.
    needs_backfill = ltf_state.pop("needs_backfill", False)
    if needs_backfill and entry.get("htf_trigger_time") is not None:
        # tz-normalize before comparing — df.index may be tz-aware (e.g.
        # yfinance returns exchange-local tz for some tickers) while
        # trigger_ts's offset, parsed back from a stored ISO string, isn't
        # guaranteed to match; comparing mismatched tz-awareness raises.
        idx = df.index.tz_localize(None) if df.index.tz is not None else df.index
        trigger_ts = pd.Timestamp(entry["htf_trigger_time"])
        if trigger_ts.tzinfo is not None:
            trigger_ts = trigger_ts.tz_localize(None)
        active_mask = idx >= trigger_ts
        # A single constant anchor (trigger_ts) for the whole active stretch
        # is correct here, not just a simplification: htf_trigger_origin
        # already found the LATEST anchor as of the latest bar (that's what
        # "origin" means post re-anchoring — see htf_bias_series), so there
        # is no later re-anchor event hiding inside this replay window.
        bias_df = pd.DataFrame({
            "bias": [entry["direction"] if m else None for m in active_mask],
            "anchor": [trigger_ts if m else None for m in active_mask],
        }, index=df.index)
        _, current_state = backtest.replay_lower_tf(df, bias_df, entry, entry["higher_tf"], ltf)
        ltf_state.update(current_state)
        ltf_state["updated_at"] = now_iso()
        ltf_state["candles"] = serialize_candles(df)
        logger.info(
            "BACKFILLED: %s %s -> %s (replayed from real trigger %s)",
            entry["display_name"], ltf, ltf_state["state"], entry["htf_trigger_time"],
        )
        return

    latest = df.iloc[-1]

    price = latest["close"]
    high = latest["high"]
    low = latest["low"]
    lsma = latest["lsma"]
    macd_hist = latest["macd_hist"]
    macd = latest["macd"]
    macd_signal = latest["macd_signal"]

    if pd.isna(lsma) or pd.isna(macd):
        return  # indicators not warmed up yet for this slice — try again next run

    direction = entry["direction"]

    # A live-but-unfilled entry order needs two checks before anything else:
    # did price reach it (fill), or did price invalidate it (a new swing
    # past the recorded risk stop before it ever filled)? Either way this
    # candle's normal pullback logic doesn't apply this run — a fill just
    # updates price/candles and stays sticky; a cancel falls through below
    # to re-track a fresh pullback under the same still-active bias.
    if ltf_state["state"] == config.STATE_TRIGGERED and not ltf_state.get("filled", False):
        stop_price = ltf_state.get("stop_price")
        filled_now = stop_price is not None and (
            (high >= stop_price) if direction == config.DIRECTION_BULLISH else (low <= stop_price)
        )
        if filled_now:
            ltf_state["filled"] = True
            ltf_state["filled_at"] = now_iso()
            ltf_state["filled_time"] = to_utc_iso(df.index[-1])
            ltf_state["updated_at"] = now_iso()
            ltf_state["price"] = round(float(price), 6)
            ltf_state["candles"] = serialize_candles(df)
            logger.info("FILLED: %s %s entry order filled @ %.6f", entry["display_name"], ltf, stop_price)
            return

        risk_stop = ltf_state.get("risk_stop_price")
        invalidated = risk_stop is not None and (
            (low < risk_stop) if direction == config.DIRECTION_BULLISH else (high > risk_stop)
        )
        if invalidated:
            logger.info(
                "CANCELLED: %s %s setup invalidated before fill (new swing past stop %.6f) — resetting to WATCHING",
                entry["display_name"], ltf, risk_stop,
            )
            ltf_state.clear()
            ltf_state.update({"state": config.STATE_WATCHING, "updated_at": now_iso()})
            # fall through — re-evaluate this same bar fresh below
        else:
            ltf_state["updated_at"] = now_iso()
            ltf_state["price"] = round(float(price), 6)
            ltf_state["candles"] = serialize_candles(df)
            return

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
        # Only reachable here already-filled (unfilled TRIGGERED is handled
        # + returned above) — sticky for the rest of the visibility window.
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

    if new_state in (config.STATE_PULLBACK, config.STATE_CONVERGING):
        # Running extreme of the whole pullback episode — this becomes the
        # risk stop-loss if/when it breaks out into TRIGGERED.
        prev_extreme = ltf_state.get("risk_stop_price")
        if direction == config.DIRECTION_BULLISH:
            ltf_state["risk_stop_price"] = round(float(low) if prev_extreme is None else min(prev_extreme, float(low)), 6)
        else:
            ltf_state["risk_stop_price"] = round(float(high) if prev_extreme is None else max(prev_extreme, float(high)), 6)

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
        ltf_state["filled"] = False
        ltf_state["trigger_time"] = to_utc_iso(latest.name)
        risk_stop = ltf_state.get("risk_stop_price")
        if risk_stop is not None:
            risk = abs(stop_price - risk_stop)
            ltf_state["target_price"] = round(
                stop_price + risk if direction == config.DIRECTION_BULLISH else stop_price - risk, 6
            )
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
