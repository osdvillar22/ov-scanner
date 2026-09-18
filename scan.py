"""
scan.py — the actual engine. Run this on a schedule (GitHub Actions later;
your own machine for now) and it will:

  1. Scan the full asset universe on 1H/4H/1D/1W for an active RSI>70 or
     RSI<30 extreme ("WATCHING"). An asset stays watchlisted as long as any
     of the last VISIBILITY_WINDOW_CANDLES candles on that timeframe had
     the extreme — a fresh extreme always re-anchors the trigger candle and
     the countdown to itself, even mid-window.
  2. For every watchlisted asset, also fetch+serialize its two mapped lower
     timeframes (config.LOWER_TF_MAP) purely as reference charts for the
     dashboard — no state or setup tracking runs on them.
  3. Persist state between runs (state.json) so an asset doesn't lose its
     progress just because you re-run the script.
  4. Write data.json for the dashboard.
  5. Fire a batched Discord alert for anything new this run.

This is Phase A only. The pullback/entry-setup state machine (Phase B) that
used to run on the lower timeframes has been removed from the live scanner;
it still exists as a standalone research tool in backtest.py.

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


def fetch_and_compute(asset: dict, tf: str) -> pd.DataFrame | None:
    """Fetch one (asset, timeframe) and run every indicator on it, or None
    if the fetch failed or came back too short to trust."""
    df = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], tf)
    if df is None or len(df) < config.MIN_WARMUP_BARS:
        return None
    return indicators.compute_all(df)


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
# Phase A — higher-timeframe RSI watchlist
# ---------------------------------------------------------------------------

def scan_higher_timeframe(asset: dict, htf: str, state: dict, watch_events: list) -> None:
    """
    Fetch + check one asset on one higher timeframe. Mutates `state` in
    place: creates a new WATCHING entry the first time an RSI-extreme
    window is active, or refreshes an existing one.

    htf_candle_count/htf_trigger_time/rsi_at_trigger/direction are all
    recomputed fresh from real data every run via backtest.htf_trigger_origin
    — not incrementally tallied. A hand-incremented counter would silently
    fall behind reality any time the scanner missed a run (a failed
    workflow, a manual re-trigger gap, etc.) — recomputing from the actual
    candle history is self-correcting regardless of any such gap, and it
    also means an asset is never missed just because the scanner happened
    to catch it on a later bar than the one that actually triggered it.
    """
    key = f"{asset['asset_class']}:{asset['ticker']}:{htf}"

    df = fetch_and_compute(asset, htf)
    if df is None:
        if key in state:
            logger.warning("Fetch too short/failed for tracked entry %s — leaving state untouched this run", key)
        return

    latest_bar_time = str(df.index[-1])
    rsi = float(df.iloc[-1]["rsi"])
    origin = backtest.htf_trigger_origin(df)

    entry = state.get(key)

    if entry is None:
        if origin is not None:
            entry = {
                "asset_class": asset["asset_class"],
                "ticker": asset["ticker"],
                "display_name": asset["display_name"],
                "higher_tf": htf,
                "direction": origin["direction"],
                "rsi_at_trigger": origin["rsi_at_trigger"],
                "first_seen": origin["trigger_time"].isoformat(),
                "htf_candle_count": origin["htf_candle_count"],
                "last_htf_bar_time": latest_bar_time,
                "htf_trigger_time": origin["trigger_time"].isoformat(),
            }
            state[key] = entry
            watch_events.append(entry)
            logger.info(
                "NEW WATCH: %s %s RSI=%.1f (%s) [real trigger %s, %d candles ago]",
                asset["display_name"], htf, rsi, origin["direction"],
                entry["htf_trigger_time"], origin["htf_candle_count"],
            )
    else:
        # Advance/refresh exactly once per new closed bar.
        new_bar_closed = entry.get("last_htf_bar_time") != latest_bar_time
        entry["last_htf_bar_time"] = latest_bar_time

        if origin is not None:
            new_trigger_iso = origin["trigger_time"].isoformat()
            re_anchored = new_bar_closed and new_trigger_iso != entry.get("htf_trigger_time")
            entry["direction"] = origin["direction"]
            entry["rsi_at_trigger"] = origin["rsi_at_trigger"]
            entry["htf_candle_count"] = origin["htf_candle_count"]
            entry["htf_trigger_time"] = new_trigger_iso
            if re_anchored:
                logger.info(
                    "RE-ANCHORED: %s %s RSI=%.1f (%s) — fresh extreme, watch window restarted",
                    asset["display_name"], htf, rsi, origin["direction"],
                )
        else:
            # No active window per fresh data — push the counter past the
            # window so expire_stale_entries drops it this run instead of
            # lingering on whatever count it last had.
            entry["htf_candle_count"] = config.VISIBILITY_WINDOW_CANDLES + 1

    if entry is not None:
        entry["last_rsi"] = round(rsi, 2)
        entry["candles"] = serialize_candles(df)
        entry["lower_tf_candles"] = {}
        for ltf in config.LOWER_TF_MAP[htf]:
            df_ltf = fetch_and_compute(asset, ltf)
            if df_ltf is not None:
                entry["lower_tf_candles"][ltf] = serialize_candles(df_ltf)


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
# Discord alerts
# ---------------------------------------------------------------------------

def send_discord_alert(watch_events: list) -> None:
    webhook_url = os.environ.get(config.DISCORD_WEBHOOK_ENV)
    if not watch_events:
        logger.info("Nothing new this run — no Discord alert sent.")
        return
    if not webhook_url:
        logger.info(
            "%s not set — skipping Discord send. Would have alerted: %d new watches.",
            config.DISCORD_WEBHOOK_ENV, len(watch_events),
        )
        return

    embeds = [{
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
    }]

    try:
        resp = requests.post(webhook_url, json={"embeds": embeds}, timeout=10)
        resp.raise_for_status()
        logger.info("Discord alert sent: %d new watches.", len(watch_events))
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

    logger.info("Phase A: higher-timeframe RSI watchlist scan (%s)", ", ".join(config.HIGHER_TIMEFRAMES))
    for asset in universe:
        for htf in config.HIGHER_TIMEFRAMES:
            scan_higher_timeframe(asset, htf, state, watch_events)

    expire_stale_entries(state)

    write_output(state)
    save_state(state)
    send_discord_alert(watch_events)


if __name__ == "__main__":
    run()
