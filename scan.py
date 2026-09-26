"""
scan.py — the actual engine. Run this on a schedule (GitHub Actions later;
your own machine for now) and it will:

  1. Scan the full asset universe on 1H/4H/1D for a watch trigger (see
     find_phase_a_watch below) — a stricter condition than a plain RSI
     extreme, ported from the user's own TradingView indicator's "black
     triangle" signal:
       - EMA10/EMA20 trend alignment
       - this timeframe's LSMA(50,3) on the correct side of the next-
         higher timeframe's LSMA(50,3) (config.AUTO_HIGHER_TF)
       - RSI(14) beyond 70/30
       - price beyond both EMAs
     all four at once, in the same direction.
  2. An asset is watchlisted if that condition fired on ANY of the
     trailing config.WATCH_WINDOW_CANDLES candles, and comes off the
     watchlist the moment NONE of them still do, OR a finished candle after
     the most recent trigger closed on the wrong side of EMA20 (trend
     break) — recomputed fresh every run. Every qualifying candle in the
     window gets marked on the dashboard, not just the first one.
     After that removal, a candle reaching SMA 50 starts a separate SMA 50
     watch for the next few candles (see find_sma50_watch).
  3. For every watchlisted asset, also fetch+serialize its two mapped lower
     timeframes (config.LOWER_TF_MAP) purely as reference charts for the
     dashboard — no state or setup tracking runs on them.
  4. Write data.json for the dashboard. state.json is kept only as a
     between-run cache for display convenience — every run fully
     recomputes each asset's watch status from real historical data, so
     state.json is never the source of truth and can't drift or go stale.
  5. Run the hourly basket check (basket.run_hourly): drop hand-picked
     basket entries whose watch is gone, alert on lower-tf entries for
     non-crypto picks, and attach every pick's status to data.json.
     New-watch Discord alerts were retired — only basket entries alert.

This is Phase A only. The pullback/entry-setup state machine (Phase B) that
used to run on the lower timeframes has been removed from the live scanner;
it still exists as a standalone research tool in backtest.py (which also
still has its own, separate, RSI-only watch-window logic — unrelated to
the condition this file implements).

Local testing: `python scan.py` with no DISCORD_ENTRY_WEBHOOK_URL set will
just log what it would have sent, instead of failing.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config
import fetch
import indicators

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scan")


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_pse_exclusions() -> set[str]:
    """Codes from config.PSE_EXCLUDED_FILE (one per line, # = comment)."""
    path = Path(config.PSE_EXCLUDED_FILE)
    if not path.exists():
        return set()
    return {line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")}


def build_universe() -> list[dict]:
    """Every (asset_class, ticker, display_name) triple we scan."""
    universe = []

    excluded = load_pse_exclusions()
    for symbol in fetch.get_pse_symbols():
        if symbol not in excluded or symbol in config.PSE_LARGE_CAPS:
            universe.append({"asset_class": "pse", "ticker": symbol, "display_name": symbol, "tag": "PSE",
                             "large_cap": symbol in config.PSE_LARGE_CAPS})

    for display_name, ticker in config.FOREX_PAIRS.items():
        universe.append({"asset_class": "forex", "ticker": ticker, "display_name": display_name})

    for display_name, ticker in config.METALS.items():
        universe.append({"asset_class": "metals", "ticker": ticker, "display_name": display_name})

    for display_name, ticker in config.INDICES.items():
        universe.append({"asset_class": "indices", "ticker": ticker, "display_name": display_name})

    for display_name, ticker in config.ENERGY.items():
        universe.append({"asset_class": "energy", "ticker": ticker, "display_name": display_name})

    for p in fetch.get_kraken_usd_pairs():
        universe.append({"asset_class": "crypto", "ticker": p["pair"], "display_name": p["display_name"], "tag": p["tag"],
                         "large_cap": p["display_name"].split("/")[0] in config.CRYPTO_LARGE_CAPS})

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
    Path(config.STATE_FILE).write_text(json.dumps(state, separators=(",", ":"), default=str))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_and_compute(asset: dict, tf: str, min_bars: int = config.MIN_WARMUP_BARS) -> pd.DataFrame | None:
    """Fetch one (asset, timeframe) and run every indicator on it, or None
    if the fetch failed or came back too short to trust. `min_bars` is
    lowered for AUTO_HIGHER_TF lookups (see find_phase_a_watch) since
    those only ever need LSMA(50), not RSI/MACD's longer warm-up."""
    df = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], tf)
    if df is None or len(df) < min_bars:
        return None
    return indicators.compute_all(df)


CANDLE_FIELDS = ["time", "open", "high", "low", "close", "ema10", "ema20", "lsma", "sma50", "macd", "macd_signal", "macd_hist"]


def _sig(value, digits: int = 7):
    """Round to significant digits, not decimal places — a fixed 6 decimals
    squashed sub-cent crypto (0.00001234 -> 1.2e-05) into stair-step charts,
    while wasting digits on large prices."""
    return None if pd.isna(value) else float(f"{float(value):.{digits}g}")


def serialize_candles(df: pd.DataFrame, n: int = config.DASHBOARD_CANDLE_WINDOW) -> dict:
    """
    Trailing window of OHLC + indicator values for dashboard.html's charts,
    as {"fields": [...], "rows": [[...], ...]} — one plain array per candle
    instead of a dict repeating every key, which was most of data.json's
    size. `time` is Unix seconds (UTC) — lightweight-charts' native format.

    The next-higher timeframe's LSMA (see _align_auto_lsma) is used inside
    the watch condition but deliberately NOT included here — plotting it
    dragged the price scale down to fit a slow-moving line far from
    current price, flattening the actual candles.
    """
    rows = []
    for ts, row in df.tail(n).iterrows():
        ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        rows.append([int(ts_utc.timestamp())] + [_sig(row[f]) for f in CANDLE_FIELDS[1:]])
    return {"fields": CANDLE_FIELDS, "rows": rows}


# ---------------------------------------------------------------------------
# Phase A — the watch-trigger condition
# ---------------------------------------------------------------------------

def _entry_direction(row: pd.Series, higher_lsma: float | None) -> str | None:
    """The "black triangle" entry condition, evaluated for one candle.
    `higher_lsma=None` means this timeframe has no next-higher timeframe
    (the PSE 1W watch) — the LSMA check is skipped, the rest still apply."""
    use_lsma = higher_lsma is not None
    if use_lsma and pd.isna(higher_lsma):
        return None
    ema10, ema20, lsma, rsi, close = row["ema10"], row["ema20"], row["lsma"], row["rsi"], row["close"]
    if pd.isna(ema10) or pd.isna(ema20) or pd.isna(rsi) or (use_lsma and pd.isna(lsma)):
        return None

    if ema10 > ema20 and (not use_lsma or lsma > higher_lsma) and rsi >= config.RSI_OVERBOUGHT and close > ema10 and close > ema20:
        return config.DIRECTION_BULLISH
    if ema10 < ema20 and (not use_lsma or lsma < higher_lsma) and rsi <= config.RSI_OVERSOLD and close < ema10 and close < ema20:
        return config.DIRECTION_BEARISH
    return None


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


def last_closed_index(df: pd.DataFrame, tf: str) -> int:
    """Index of the most recent FINISHED candle. Both Kraken and yfinance
    include the still-forming current candle as the last row; a bar is
    finished once its open time + its length is in the past."""
    last_open = df.index[-1]
    last_open = last_open.tz_localize("UTC") if last_open.tzinfo is None else last_open.tz_convert("UTC")
    closes_at = last_open + pd.Timedelta(minutes=config.TIMEFRAME_MINUTES[tf])
    return len(df) - 1 if closes_at <= pd.Timestamp.now(tz="UTC") else len(df) - 2


def find_phase_a_watch(
    df_htf: pd.DataFrame, aligned_auto_lsma: pd.Series | None, last_closed: int,
    window: int = config.WATCH_WINDOW_CANDLES,
) -> dict | None:
    """
    Look only at the trailing `window` candles — no anchor, no expiry, just
    "did the entry condition fire on any of them." Recomputed fresh from
    real data every call.

    `aligned_auto_lsma` is the next-higher timeframe's LSMA already
    backward-aligned onto df_htf's index (see _align_auto_lsma), or None
    for a timeframe without one (PSE 1W) — then the LSMA check is skipped.
    `last_closed` is the index of the most recent finished candle (see
    last_closed_index) — the trend-break check below ignores anything after
    it, so an intrabar dip through EMA20 can't flap an asset off and back
    on (with a repeat Discord alert) before the candle has actually closed.

    If the window contains qualifying candles in BOTH directions (rare —
    e.g. a sharp reversal), direction is taken from the most recent
    qualifying candle, and only candles matching that same direction are
    reported/marked; an older, opposite-direction match is treated as
    superseded context rather than still-relevant.

    Returns None if zero candles in the window qualify. If they do but a
    finished candle AFTER the most recent trigger closed on the wrong side
    of EMA20 (below it for bullish, above for bearish), returns the watch
    with `invalidated_at` set — the trend broke, so the caller drops it. A
    newer trigger after the break starts fresh, since only candles after
    the most recent trigger are checked.
    """
    n = len(df_htf)
    start = max(0, n - window)
    qualifying = [
        (i, d) for i in range(start, n)
        if (d := _entry_direction(df_htf.iloc[i], None if aligned_auto_lsma is None else aligned_auto_lsma.iloc[i])) is not None
    ]
    if not qualifying:
        return None

    direction = qualifying[-1][1]
    matching_idx = [i for i, d in qualifying if d == direction]
    most_recent = matching_idx[-1]

    invalidated_at = None
    for i in range(most_recent + 1, last_closed + 1):
        close, ema20 = df_htf.iloc[i]["close"], df_htf.iloc[i]["ema20"]
        broke = close < ema20 if direction == config.DIRECTION_BULLISH else close > ema20
        if broke:
            invalidated_at = df_htf.index[i]
            break

    return {
        "direction": direction,
        "trigger_indices": matching_idx,
        "trigger_times": [df_htf.index[i] for i in matching_idx],
        "qualifying_count": len(matching_idx),
        "window": n - start,
        "candles_ago_most_recent": (n - 1) - most_recent,
        "rsi_at_trigger": round(float(df_htf.iloc[most_recent]["rsi"]), 2),
        "invalidated_at": invalidated_at,
    }


# ---------------------------------------------------------------------------
# Phase A — per-asset scan
# ---------------------------------------------------------------------------

def find_sma50_watch(
    df_htf: pd.DataFrame, aligned_auto_lsma: pd.Series | None, last_closed: int,
    window: int = config.WATCH_WINDOW_CANDLES,
) -> dict | None:
    """
    The SMA 50 pullback watch. Replays the trend watch's own life cycle over
    recent candles — trigger(s), then the removal candle (a finished candle
    closing past EMA20 while the last trigger is still in its window) — and
    looks from that removal candle (included) through the next
    SMA50_SEARCH_CANDLES for a candle reaching SMA 50: low within
    SMA50_TOUCH_ATR x ATR14 above it, or through it (bullish; mirrored for
    bearish). The watch lasts SMA50_WATCH_CANDLES candles counted from that
    touch candle — whether price then bounces or goes under SMA 50.

    The still-forming candle can be the touch: once its low has reached the
    level it can't un-reach it. Returns the live watch, or None.
    """
    n = len(df_htf)
    search, life = config.SMA50_SEARCH_CANDLES, config.SMA50_WATCH_CANDLES
    start = max(1, n - (window + search + life + 20))
    last_trig, direction, trig_idx, found = None, None, [], None
    for i in range(start, last_closed + 1):
        row = df_htf.iloc[i]
        d = _entry_direction(row, None if aligned_auto_lsma is None else aligned_auto_lsma.iloc[i])
        if d:
            if last_trig is None or d != direction:
                trig_idx = []
            last_trig, direction = i, d
            trig_idx.append(i)
            continue
        if last_trig is None:
            continue
        if i - last_trig >= window:  # aged out of the window — not a removal
            last_trig = None
            continue
        bull = direction == config.DIRECTION_BULLISH
        if not ((row["close"] < row["ema20"]) if bull else (row["close"] > row["ema20"])):
            continue
        last_trig = None  # removed
        for j in range(i, min(i + search, n)):
            c = df_htf.iloc[j]
            if pd.isna(c["sma50"]) or pd.isna(c["atr"]):
                continue
            reach = config.SMA50_TOUCH_ATR * c["atr"]
            if (c["low"] <= c["sma50"] + reach) if bull else (c["high"] >= c["sma50"] - reach):
                if (n - 1) - j < life:
                    found = {
                        "direction": direction,
                        "trigger_times": [df_htf.index[k] for k in trig_idx],
                        "removed_at": df_htf.index[i],
                        "touch_at": df_htf.index[j],
                        "touch_gap": j - i,
                        "candles_since_touch": (n - 1) - j,
                        "candles_left": life - ((n - 1) - j),
                    }
                break
    return found


def _fill_charts(entry: dict, asset: dict, htf: str, df_htf: pd.DataFrame, ltf_cache: dict) -> None:
    """Chart data for one watch: its own timeframe plus both lower tfs,
    fetched once per asset even when a tf has both a trend and an SMA 50
    watch. Also refreshes naming, so renames reach existing watches."""
    entry["display_name"] = asset["display_name"]
    entry["tag"] = asset.get("tag")
    entry["large_cap"] = bool(asset.get("large_cap"))
    entry["last_rsi"] = round(float(df_htf.iloc[-1]["rsi"]), 2)
    entry["candles"] = serialize_candles(df_htf)
    entry["lower_tf_candles"] = {}
    for ltf in config.LOWER_TF_MAP[htf]:
        if ltf not in ltf_cache:
            ltf_cache[ltf] = fetch_and_compute(asset, ltf)
        if ltf_cache[ltf] is not None:
            entry["lower_tf_candles"][ltf] = serialize_candles(ltf_cache[ltf], n=lower_tf_candle_count(htf, ltf))


def scan_higher_timeframe(
    asset: dict, htf: str, df_htf: pd.DataFrame | None, df_auto: pd.DataFrame | None, state: dict,
    ltf_cache: dict | None = None,
) -> None:
    """Check one asset on one higher timeframe using its (already-fetched)
    own data and its AUTO_HIGHER_TF data, and mutate `state` accordingly:
    add, refresh, or remove — the trend watch, then the SMA 50 watch."""
    key = f"{asset['asset_class']}:{asset['ticker']}:{htf}"
    has_auto = htf in config.AUTO_HIGHER_TF
    ltf_cache = {} if ltf_cache is None else ltf_cache

    if df_htf is None or (has_auto and df_auto is None):
        for k in (key, f"{key}:sma50"):
            if k in state:
                logger.warning("Fetch too short/failed for tracked entry %s — leaving state untouched this run", k)
        return

    aligned_auto_lsma = _align_auto_lsma(df_htf, df_auto) if has_auto else None
    last_closed = last_closed_index(df_htf, htf)
    _scan_trend(asset, htf, df_htf, aligned_auto_lsma, last_closed, state, key, ltf_cache)
    _scan_sma50(asset, htf, df_htf, aligned_auto_lsma, last_closed, state, f"{key}:sma50", ltf_cache)


def _scan_sma50(asset, htf, df_htf, aligned_auto_lsma, last_closed, state, key, ltf_cache) -> None:
    if (asset["asset_class"], htf) in config.SMA50_EXCLUDED:
        state.pop(key, None)
        return
    watch = find_sma50_watch(df_htf, aligned_auto_lsma, last_closed)
    if watch and asset["asset_class"] in config.SMA50_BULLISH_ONLY and watch["direction"] != config.DIRECTION_BULLISH:
        watch = None
    entry = state.get(key)
    if watch is None:
        if entry is not None:
            logger.info("REMOVED: %s %s SMA 50 — %d candles since the touch", asset["display_name"], htf, config.SMA50_WATCH_CANDLES)
            del state[key]
        return
    if entry is None or entry.get("touch_at") != watch["touch_at"].isoformat():
        entry = {
            "asset_class": asset["asset_class"], "ticker": asset["ticker"], "higher_tf": htf,
            "setup": "sma50", "first_seen": now_iso(),
        }
        state[key] = entry
        logger.info("NEW SMA 50 WATCH: %s %s (%s) touched %d candles after the EMA20 removal",
                    asset["display_name"], htf, watch["direction"], watch["touch_gap"])
    entry["direction"] = watch["direction"]
    entry["trigger_times"] = [t.isoformat() for t in watch["trigger_times"]]
    entry["removed_at"] = watch["removed_at"].isoformat()
    entry["touch_at"] = watch["touch_at"].isoformat()
    entry["touch_gap"] = watch["touch_gap"]
    entry["candles_ago_most_recent"] = watch["candles_since_touch"]
    entry["candles_left"] = watch["candles_left"]
    entry["life"] = config.SMA50_WATCH_CANDLES
    entry["window"] = config.WATCH_WINDOW_CANDLES
    _fill_charts(entry, asset, htf, df_htf, ltf_cache)


def _scan_trend(asset, htf, df_htf, aligned_auto_lsma, last_closed, state, key, ltf_cache) -> None:
    entry = state.get(key)
    watch = find_phase_a_watch(df_htf, aligned_auto_lsma, last_closed)

    if watch is None or watch["invalidated_at"] is not None:
        if entry is not None:
            if watch is None:
                reason = f"none of the last {config.WATCH_WINDOW_CANDLES} candles still qualify"
            else:
                side = "below" if watch["direction"] == config.DIRECTION_BULLISH else "above"
                reason = f"candle at {watch['invalidated_at']} closed {side} EMA20 after the last trigger"
            logger.info("REMOVED: %s %s — %s", asset["display_name"], htf, reason)
            del state[key]
        return

    if entry is None:
        entry = {
            "asset_class": asset["asset_class"],
            "ticker": asset["ticker"],
            "display_name": asset["display_name"],
            "higher_tf": htf,
            "setup": "trend",
            "auto_higher_tf": config.AUTO_HIGHER_TF.get(htf),
            "first_seen": now_iso(),
        }
        state[key] = entry
        logger.info(
            "NEW WATCH: %s %s (%s) RSI=%.1f [%d/%d candles qualify, most recent %d candles ago]",
            asset["display_name"], htf, watch["direction"], watch["rsi_at_trigger"],
            watch["qualifying_count"], watch["window"], watch["candles_ago_most_recent"],
        )

    # Refreshed every run, not just at creation.
    entry["setup"] = "trend"
    entry["direction"] = watch["direction"]
    entry["rsi_at_trigger"] = watch["rsi_at_trigger"]
    entry["qualifying_count"] = watch["qualifying_count"]
    entry["window"] = watch["window"]
    entry["candles_ago_most_recent"] = watch["candles_ago_most_recent"]
    entry["trigger_times"] = [t.isoformat() for t in watch["trigger_times"]]
    _fill_charts(entry, asset, htf, df_htf, ltf_cache)


def lower_tf_candle_count(htf: str, ltf: str) -> int:
    """How many trailing candles a lower-tf reference chart should show, so
    it spans the same real time as WATCH_WINDOW_CANDLES on the higher
    timeframe. Both lower tfs get the full equivalent — the dashboard's
    charts are zoomable, so the deepest tf's large count no longer clutters."""
    span_minutes = config.WATCH_WINDOW_CANDLES * config.TIMEFRAME_MINUTES[htf]
    return round(span_minutes / config.TIMEFRAME_MINUTES[ltf])


def higher_timeframes_for(asset: dict) -> list[str]:
    """1H/4H/1D for everything, plus 1W for PSE and crypto."""
    return config.HIGHER_TIMEFRAMES + config.EXTRA_HIGHER_TIMEFRAMES.get(asset["asset_class"], [])


def scan_asset(asset: dict, state: dict) -> None:
    """Run every higher-timeframe check for one asset. Each needed
    timeframe — including AUTO_HIGHER_TF lookups, several of which overlap
    with another timeframe's own scan (e.g. "4H" is both 1H's auto-tf and
    4H's own scan, and PSE's 1W is both 1D's auto-tf and its own watch) —
    is fetched at most once per asset per run."""
    htfs = higher_timeframes_for(asset)
    tf_data: dict[str, pd.DataFrame | None] = {}
    for htf in htfs:
        tf_data[htf] = fetch_and_compute(asset, htf, min_bars=config.MIN_BARS_BY_TF.get(htf, config.MIN_WARMUP_BARS))

    auto_tfs = {config.AUTO_HIGHER_TF[h] for h in htfs if h in config.AUTO_HIGHER_TF} - set(tf_data)
    for tf in auto_tfs:
        tf_data[tf] = fetch_and_compute(asset, tf, min_bars=config.LSMA_WARMUP_BARS)

    # Lower-tf charts reuse a frame already fetched here (e.g. 1H for 4H).
    ltf_cache = {tf: df for tf, df in tf_data.items() if tf in htfs and df is not None}
    for htf in htfs:
        df_auto = tf_data.get(config.AUTO_HIGHER_TF.get(htf))
        scan_higher_timeframe(asset, htf, tf_data[htf], df_auto, state, ltf_cache)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(state: dict, basket_status: dict) -> None:
    Path(config.OUTPUT_FILE).write_text(json.dumps({
        "generated_at": now_iso(),
        "assets": list(state.values()),
        "basket_status": basket_status,
    }, separators=(",", ":"), default=str))
    logger.info("Wrote %s with %d active entries.", config.OUTPUT_FILE, len(state))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prune_unscanned(state: dict, universe: list) -> None:
    """Drop watches for assets no longer in the universe (delisted, or
    excluded like the stablecoins) — scan_asset never visits them again, so
    they'd otherwise sit on the dashboard forever. An asset class that came
    back empty this run (e.g. Kraken's pair list failed to load) is left
    untouched rather than wiped."""
    scanned = {(a["asset_class"], a["ticker"]) for a in universe}
    classes_present = {a["asset_class"] for a in universe}
    for key, entry in list(state.items()):
        if entry["asset_class"] in classes_present and (entry["asset_class"], entry["ticker"]) not in scanned:
            logger.info("REMOVED: %s %s — no longer in the scanned universe", entry["display_name"], entry["higher_tf"])
            del state[key]


def run() -> None:
    import basket  # imports scan itself — deferred to avoid a circular import

    state = load_state()
    universe = build_universe()

    logger.info("Phase A: higher-timeframe watch-trigger scan (%s)", ", ".join(config.HIGHER_TIMEFRAMES))
    for asset in universe:
        scan_asset(asset, state)

    prune_unscanned(state, universe)
    basket_status = basket.run_hourly(state)
    write_output(state, basket_status)
    save_state(state)


if __name__ == "__main__":
    run()
