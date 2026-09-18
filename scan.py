"""
scan.py — the actual engine. Run this on a schedule (GitHub Actions later;
your own machine for now) and it will:

  1. Scan the full asset universe on 1H/4H/1D/1W for a currently-active
     watch trigger (see find_phase_a_origin below) — a stricter condition
     than a plain RSI extreme, ported from the user's own TradingView
     indicator's "black triangle" signal:
       - EMA10/EMA20 trend alignment
       - this timeframe's LSMA(50,3) on the correct side of the next-
         higher timeframe's LSMA(50,3) (config.AUTO_HIGHER_TF)
       - RSI(14) beyond 70/30
       - price beyond both EMAs
     all four at once, in the same direction.
  2. Once watchlisted, there is NO fixed expiry — an asset only comes off
     the watchlist when price closes back through BOTH the current-
     timeframe LSMA(50,3) and EMA20 (the mirror condition for the
     opposite direction). A fresh trigger on a later candle re-anchors the
     watch to itself, same idea as before but driven by this richer
     condition instead of RSI alone.
  3. For every watchlisted asset, also fetch+serialize its two mapped lower
     timeframes (config.LOWER_TF_MAP) purely as reference charts for the
     dashboard — no state or setup tracking runs on them.
  4. Write data.json for the dashboard. state.json is kept only as a
     between-run cache for display convenience — every run fully
     recomputes each asset's watch status from real historical data, so
     state.json is never the source of truth and can't drift or go stale.
  5. Fire a batched Discord alert for anything new this run.

This is Phase A only. The pullback/entry-setup state machine (Phase B) that
used to run on the lower timeframes has been removed from the live scanner;
it still exists as a standalone research tool in backtest.py (which also
still has its own, separate, RSI-only watch-window logic — unrelated to
the condition this file implements).

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


def fetch_and_compute(asset: dict, tf: str, min_bars: int = config.MIN_WARMUP_BARS) -> pd.DataFrame | None:
    """Fetch one (asset, timeframe) and run every indicator on it, or None
    if the fetch failed or came back too short to trust. `min_bars` is
    lowered for AUTO_HIGHER_TF lookups (see find_phase_a_origin) since
    those only ever need LSMA(50), not RSI/MACD's longer warm-up."""
    df = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], tf)
    if df is None or len(df) < min_bars:
        return None
    return indicators.compute_all(df)


def serialize_candles(df: pd.DataFrame, n: int = config.DASHBOARD_CANDLE_WINDOW, lsma2: pd.Series | None = None) -> list:
    """
    Trailing window of OHLC + indicator values for dashboard.html's charts.
    `time` is Unix seconds (UTC) — lightweight-charts' native format.

    `lsma2`, when given, is the next-higher timeframe's LSMA already
    aligned onto df's index (see _align_auto_lsma) — only meaningful for
    the trigger-timeframe chart, since it's what the watch condition
    itself compares against.
    """
    tail = df.tail(n)
    lsma2_tail = lsma2.tail(n) if lsma2 is not None else None
    candles = []
    for i, (ts, row) in enumerate(tail.iterrows()):
        ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        candle = {
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
        }
        if lsma2_tail is not None:
            v = lsma2_tail.iloc[i]
            candle["lsma2"] = None if pd.isna(v) else round(float(v), 6)
        candles.append(candle)
    return candles


# ---------------------------------------------------------------------------
# Phase A — the watch-trigger condition
# ---------------------------------------------------------------------------

def _entry_direction(row: pd.Series, higher_lsma: float) -> str | None:
    """The "black triangle" entry condition, evaluated for one candle."""
    if pd.isna(higher_lsma):
        return None
    ema10, ema20, lsma, rsi, close = row["ema10"], row["ema20"], row["lsma"], row["rsi"], row["close"]
    if pd.isna(ema10) or pd.isna(ema20) or pd.isna(lsma) or pd.isna(rsi):
        return None

    if ema10 > ema20 and lsma > higher_lsma and rsi >= config.RSI_OVERBOUGHT and close > ema10 and close > ema20:
        return config.DIRECTION_BULLISH
    if ema10 < ema20 and lsma < higher_lsma and rsi <= config.RSI_OVERSOLD and close < ema10 and close < ema20:
        return config.DIRECTION_BEARISH
    return None


def _is_invalidated(row: pd.Series, direction: str) -> bool:
    """The removal condition: price closes back through BOTH the current-
    timeframe LSMA and EMA20 (mirrored for the opposite direction)."""
    lsma, ema20, close = row["lsma"], row["ema20"], row["close"]
    if pd.isna(lsma) or pd.isna(ema20):
        return False
    if direction == config.DIRECTION_BULLISH:
        return close < lsma and close < ema20
    return close > lsma and close > ema20


def _align_auto_lsma(df_htf: pd.DataFrame, df_auto: pd.DataFrame) -> pd.Series:
    """Backward-align the next-higher timeframe's LSMA onto df_htf's index
    — each htf bar only ever sees the most recently CLOSED auto-tf bar's
    LSMA as of that time, never a future one (no lookahead)."""
    idx_htf = df_htf.index.tz_localize(None) if df_htf.index.tz is not None else df_htf.index
    idx_auto = df_auto.index.tz_localize(None) if df_auto.index.tz is not None else df_auto.index
    merged = pd.merge_asof(
        pd.DataFrame({"t": idx_htf}).sort_values("t"),
        pd.DataFrame({"t": idx_auto, "lsma_auto": df_auto["lsma"].values}).sort_values("t"),
        on="t", direction="backward",
    )
    return pd.Series(merged["lsma_auto"].values, index=df_htf.index)


def find_phase_a_origin(df_htf: pd.DataFrame, aligned_auto_lsma: pd.Series) -> dict | None:
    """
    Walk the full fetched history to determine whether an asset is
    CURRENTLY watchlisted, and if so, since which candle. Entirely
    stateless — recomputed fresh from real data every call, so it can't
    drift out of sync the way an incrementally-tallied counter could.

    `aligned_auto_lsma` is the next-higher timeframe's LSMA already backward-
    aligned onto df_htf's index (see _align_auto_lsma) — the caller builds
    it once and reuses it for both this walk and the dashboard's "LSMA2"
    chart series, rather than recomputing it twice.

    A fresh entry condition on a later candle re-anchors the trigger to
    itself (even immediately after an invalidation on the very same bar —
    a sharp reversal candle can invalidate one direction and trigger the
    other simultaneously). Returns None if not currently active: either
    the condition has never fired within the fetched history, or it fired
    and has since been invalidated.
    """
    active = False
    direction = None
    origin_idx = None

    n = len(df_htf)
    for i in range(n):
        row = df_htf.iloc[i]

        if active and _is_invalidated(row, direction):
            active, direction, origin_idx = False, None, None

        entry_dir = _entry_direction(row, aligned_auto_lsma.iloc[i])
        if entry_dir is not None and entry_dir != direction:
            # A genuinely fresh start — either from inactive, or a direction
            # flip. If the condition is simply STILL true in the same
            # direction as the previous bar (a sustained run), don't slide
            # the origin forward every single bar — it should stay pinned
            # to when the run actually started.
            active, direction, origin_idx = True, entry_dir, i

    if not active:
        return None
    return {
        "direction": direction,
        "trigger_time": df_htf.index[origin_idx],
        "rsi_at_trigger": round(float(df_htf.iloc[origin_idx]["rsi"]), 2),
        "candles_since_trigger": (n - 1) - origin_idx,
    }


# ---------------------------------------------------------------------------
# Phase A — per-asset scan
# ---------------------------------------------------------------------------

def scan_higher_timeframe(
    asset: dict, htf: str, df_htf: pd.DataFrame | None, df_auto: pd.DataFrame | None,
    state: dict, watch_events: list,
) -> None:
    """Check one asset on one higher timeframe using its (already-fetched)
    own data and its AUTO_HIGHER_TF data, and mutate `state` accordingly:
    add, refresh, re-anchor, or remove."""
    key = f"{asset['asset_class']}:{asset['ticker']}:{htf}"
    entry = state.get(key)

    if df_htf is None or df_auto is None:
        if entry is not None:
            logger.warning("Fetch too short/failed for tracked entry %s — leaving state untouched this run", key)
        return

    aligned_auto_lsma = _align_auto_lsma(df_htf, df_auto)
    origin = find_phase_a_origin(df_htf, aligned_auto_lsma)

    if origin is None:
        if entry is not None:
            logger.info(
                "REMOVED: %s %s — price closed back through LSMA+EMA20, condition invalidated",
                asset["display_name"], htf,
            )
            del state[key]
        return

    new_trigger_iso = origin["trigger_time"].isoformat()

    if entry is None:
        entry = {
            "asset_class": asset["asset_class"],
            "ticker": asset["ticker"],
            "display_name": asset["display_name"],
            "higher_tf": htf,
            "auto_higher_tf": config.AUTO_HIGHER_TF[htf],
            "direction": origin["direction"],
            "rsi_at_trigger": origin["rsi_at_trigger"],
            "first_seen": new_trigger_iso,
            "candles_since_trigger": origin["candles_since_trigger"],
            "htf_trigger_time": new_trigger_iso,
        }
        state[key] = entry
        watch_events.append(entry)
        logger.info(
            "NEW WATCH: %s %s (%s) RSI=%.1f [trigger %s, %d candles ago]",
            asset["display_name"], htf, origin["direction"], origin["rsi_at_trigger"],
            new_trigger_iso, origin["candles_since_trigger"],
        )
    else:
        re_anchored = new_trigger_iso != entry.get("htf_trigger_time")
        entry["direction"] = origin["direction"]
        entry["rsi_at_trigger"] = origin["rsi_at_trigger"]
        entry["candles_since_trigger"] = origin["candles_since_trigger"]
        entry["htf_trigger_time"] = new_trigger_iso
        if re_anchored:
            logger.info(
                "RE-ANCHORED: %s %s (%s) — fresh trigger, watch restarted",
                asset["display_name"], htf, origin["direction"],
            )

    entry["last_rsi"] = round(float(df_htf.iloc[-1]["rsi"]), 2)
    entry["candles"] = serialize_candles(df_htf, lsma2=aligned_auto_lsma)
    entry["lower_tf_candles"] = {}
    for ltf in config.LOWER_TF_MAP[htf]:
        df_ltf = fetch_and_compute(asset, ltf)
        if df_ltf is not None:
            entry["lower_tf_candles"][ltf] = serialize_candles(df_ltf)


def scan_asset(asset: dict, state: dict, watch_events: list) -> None:
    """Run all 4 higher-timeframe checks for one asset. Each needed
    timeframe — including AUTO_HIGHER_TF lookups, several of which overlap
    with another timeframe's own scan (e.g. "4H" is both 1H's auto-tf and
    4H's own scan) — is fetched at most once per asset per run."""
    tf_data: dict[str, pd.DataFrame | None] = {}
    for htf in config.HIGHER_TIMEFRAMES:
        tf_data[htf] = fetch_and_compute(asset, htf)

    auto_tfs = set(config.AUTO_HIGHER_TF.values()) - set(tf_data)
    for tf in auto_tfs:
        tf_data[tf] = fetch_and_compute(asset, tf, min_bars=config.LSMA_WARMUP_BARS)

    for htf in config.HIGHER_TIMEFRAMES:
        df_auto = tf_data.get(config.AUTO_HIGHER_TF[htf])
        scan_higher_timeframe(asset, htf, tf_data[htf], df_auto, state, watch_events)


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

    logger.info("Phase A: higher-timeframe watch-trigger scan (%s)", ", ".join(config.HIGHER_TIMEFRAMES))
    for asset in universe:
        scan_asset(asset, state, watch_events)

    write_output(state)
    save_state(state)
    send_discord_alert(watch_events)


if __name__ == "__main__":
    run()
