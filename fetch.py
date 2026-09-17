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
# Binance (crypto) — native support for every timeframe we use, no resampling
# ---------------------------------------------------------------------------

def get_binance_usdt_pairs() -> list[str]:
    """
    Dynamically resolve the current USDT pair universe from Binance's
    exchangeInfo endpoint, so new listings are picked up automatically
    with zero config changes (per learnings.md).
    """
    url = f"{config.BINANCE_BASE_URL}/api/v3/exchangeInfo"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        symbols = resp.json()["symbols"]
        return [
            s["symbol"]
            for s in symbols
            if s.get("quoteAsset") == config.BINANCE_QUOTE_ASSET
            and s.get("status") == "TRADING"
        ]
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve Binance USDT pairs: %s", exc)
        return []


def fetch_binance_klines(symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame | None:
    """Fetch klines for one symbol/timeframe. limit=500 comfortably covers MIN_WARMUP_BARS."""
    interval = config.BINANCE_NATIVE_INTERVAL.get(timeframe)
    if interval is None:
        logger.error("No Binance interval mapping for timeframe %s", timeframe)
        return None

    url = f"{config.BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            return None
        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ],
        )
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df = df.set_index("open_time")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Binance fetch failed for %s@%s: %s", symbol, timeframe, exc)
        return None


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def fetch_ohlc(asset_class: str, symbol_or_ticker: str, timeframe: str) -> pd.DataFrame | None:
    """
    Single entry point scan.py should call, regardless of asset class.

    asset_class: one of "forex", "metals", "crypto"
    symbol_or_ticker: the yfinance ticker (forex/metals) or Binance
                       symbol (crypto) — NOT the display name.
    """
    if asset_class == "crypto":
        return fetch_binance_klines(symbol_or_ticker, timeframe)
    if asset_class in ("forex", "metals"):
        return fetch_yfinance_ohlc(symbol_or_ticker, timeframe)

    logger.error("Unknown asset class: %s", asset_class)
    return None
