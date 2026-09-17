"""
fetch.py — turns (asset, timeframe) into a clean OHLC pandas DataFrame,
regardless of which data source that asset actually lives on.

Design note: every function here either returns a DataFrame with at least
['open','high','low','close'] columns and a datetime index, or None if the
fetch failed — it never raises for a bad/missing ticker. scan.py is
responsible for skipping-and-logging a None result rather than crashing
the whole run. That resilience is the actual hard part of this system
(see learnings.md); don't relax it while "cleaning up" this file later.
"""

import time
import logging

import pandas as pd
import requests
import yfinance as yf

import config

logger = logging.getLogger("fetch")


# ---------------------------------------------------------------------------
# yfinance (forex, metals)
# ---------------------------------------------------------------------------

def _fetch_yfinance_raw(ticker: str, interval: str) -> pd.DataFrame | None:
    """Single yfinance download with basic retry, for one native interval."""
    period = config.YFINANCE_MAX_INTRADAY_PERIOD.get(interval, "max")
    for attempt in range(3):
        try:
            df = yf.download(
                ticker,
                interval=interval,
                period=period,
                progress=False,
                auto_adjust=True,
            )
            if df is None or df.empty:
                return None
            # yfinance sometimes returns MultiIndex columns for a single
            # ticker depending on version — flatten defensively.
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            return df
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see module docstring
            logger.warning("yfinance fetch failed for %s@%s (attempt %d): %s", ticker, interval, attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    return None


def resample_to_4h(df_1h: pd.DataFrame, session_anchor_hour: int = 9) -> pd.DataFrame:
    """
    4H isn't a native yfinance interval, so we build it from 1H bars.

    Anchored to `session_anchor_hour` (local exchange open) rather than UTC
    midnight, so 4H boundaries line up with actual trading sessions instead
    of falling mid-session.
    """
    if df_1h is None or df_1h.empty:
        return df_1h

    offset = pd.Timedelta(hours=session_anchor_hour % 4)
    # Lowercase "4h" — pandas 2.2+ deprecated the uppercase "H" alias.
    resampled = df_1h.resample("4h", offset=offset).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return resampled.dropna(subset=["open", "high", "low", "close"])


def fetch_yfinance_ohlc(ticker: str, timeframe: str) -> pd.DataFrame | None:
    """Public entry point for any yfinance-backed asset (forex/metals)."""
    if timeframe == "4H":
        df_1h = _fetch_yfinance_raw(ticker, config.YFINANCE_NATIVE_INTERVAL["1H"])
        return resample_to_4h(df_1h)

    native_interval = config.YFINANCE_NATIVE_INTERVAL.get(timeframe)
    if native_interval is None:
        logger.error("No yfinance interval mapping for timeframe %s", timeframe)
        return None
    return _fetch_yfinance_raw(ticker, native_interval)


# ---------------------------------------------------------------------------
# Kraken (crypto) — native support for every timeframe we use, no resampling
# ---------------------------------------------------------------------------

def get_kraken_usd_pairs() -> list[str]:
    """
    Dynamically resolve the current USD pair universe from Kraken's
    AssetPairs endpoint, so new listings are picked up automatically with
    zero config changes. Each dict key (e.g. "PAXGUSD") is directly usable
    as the `pair` param for the OHLC endpoint below — verified against a
    random sample of the full listing, including legacy-named pairs like
    "XXMRZUSD".
    """
    url = f"{config.KRAKEN_BASE_URL}/0/public/AssetPairs"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        pairs = resp.json()["result"]
        return [
            name
            for name, info in pairs.items()
            if info.get("quote") == config.KRAKEN_QUOTE_ASSET
            and info.get("status") == "online"
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve Kraken USD pairs: %s", exc)
        return []


def fetch_kraken_ohlc(pair: str, timeframe: str) -> pd.DataFrame | None:
    """Fetch OHLC candles for one Kraken pair/timeframe. Returns 350-720 bars
    depending on interval — comfortably above MIN_WARMUP_BARS either way."""
    interval = config.KRAKEN_NATIVE_INTERVAL.get(timeframe)
    if interval is None:
        logger.error("No Kraken interval mapping for timeframe %s", timeframe)
        return None

    url = f"{config.KRAKEN_BASE_URL}/0/public/OHLC"
    params = {"pair": pair, "interval": interval}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("error"):
            logger.warning("Kraken fetch failed for %s@%s: %s", pair, timeframe, payload["error"])
            return None
        result = payload.get("result", {})
        # `result` has one key holding the candles — Kraken's own name for
        # the pair, which can differ slightly from the requested altname —
        # plus a "last" key we don't want.
        candle_keys = [k for k in result if k != "last"]
        if not candle_keys:
            return None
        raw = result[candle_keys[0]]
        if not raw:
            return None
        df = pd.DataFrame(
            raw,
            columns=["open_time", "open", "high", "low", "close", "vwap", "volume", "trades"],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="s")
        df = df.set_index("open_time")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Kraken fetch failed for %s@%s: %s", pair, timeframe, exc)
        return None


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def fetch_ohlc(asset_class: str, symbol_or_ticker: str, timeframe: str) -> pd.DataFrame | None:
    """
    Single entry point scan.py should call, regardless of asset class.

    asset_class: one of "forex", "metals", "crypto"
    symbol_or_ticker: the yfinance ticker (forex/metals) or Kraken pair
                       (crypto) — NOT the display name.
    """
    if asset_class == "crypto":
        return fetch_kraken_ohlc(symbol_or_ticker, timeframe)
    if asset_class in ("forex", "metals"):
        return fetch_yfinance_ohlc(symbol_or_ticker, timeframe)

    logger.error("Unknown asset class: %s", asset_class)
    return None
