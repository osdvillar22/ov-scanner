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

import re
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

def get_kraken_usd_pairs() -> list[dict]:
    """
    Dynamically resolve the current USD pair universe from Kraken's
    AssetPairs endpoint, so new listings are picked up automatically with
    zero config changes. Each "pair" (e.g. "PAXGUSD") is directly usable
    as the `pair` param for the OHLC endpoint below — verified against a
    random sample of the full listing, including legacy-named pairs like
    "XXMRZUSD".

    Returns [{"pair": "XXBTZUSD", "display_name": "BTC/USD", "tag": None}, ...]
    — display name from Kraken's readable "wsname", tag from
    config.KRAKEN_ASSET_TAGS for the non-crypto ones (currency, gold, ...).
    Stablecoins (config.KRAKEN_EXCLUDED_BASES) are left out.
    """
    url = f"{config.KRAKEN_BASE_URL}/0/public/AssetPairs"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        pairs = resp.json()["result"]
        out = []
        for name, info in pairs.items():
            if info.get("quote") != config.KRAKEN_QUOTE_ASSET or info.get("status") != "online":
                continue
            base = (info.get("wsname") or name).split("/")[0]
            if base in config.KRAKEN_EXCLUDED_BASES:
                continue
            display = f"{config.KRAKEN_BASE_ALIASES.get(base, base)}/USD" if info.get("wsname") else name
            out.append({"pair": name, "display_name": display, "tag": config.KRAKEN_ASSET_TAGS.get(base)})
        return out
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to resolve Kraken USD pairs: %s", exc)
        return []


_KRAKEN_CACHE_SECONDS = 120
_kraken_daily_cache: dict = {}


def fetch_kraken_ohlc(pair: str, timeframe: str) -> pd.DataFrame | None:
    """Kraken candles for any timeframe. 1W is built from the 1D candles
    (Monday weeks, like TradingView — Kraken's own start on Thursday); the
    1D frame is shared briefly so 1D + 1W of a pair cost one request."""
    if timeframe not in ("1D", "1W"):
        return _fetch_kraken_raw(pair, timeframe)
    now = time.time()
    for k in [k for k, (t, _) in _kraken_daily_cache.items() if now - t > _KRAKEN_CACHE_SECONDS]:
        del _kraken_daily_cache[k]
    hit = _kraken_daily_cache.get(pair)
    df = hit[1] if hit else _fetch_kraken_raw(pair, "1D")
    if df is None:
        return None
    _kraken_daily_cache[pair] = (now, df)
    if timeframe == "1D":
        return df
    ohlcv = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample("W-MON", label="left", closed="left").agg(ohlcv).dropna(subset=["open", "high", "low", "close"])
    # Kraken's ~720-day cap usually starts mid-week: drop that partial week.
    if len(out) and out.index[0] < df.index[0]:
        out = out.iloc[1:]
    return out


def _fetch_kraken_raw(pair: str, timeframe: str) -> pd.DataFrame | None:
    """One Kraken OHLC request (native intervals only). Returns up to 720
    bars — comfortably above MIN_WARMUP_BARS for the intraday/daily tfs."""
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
# PSE — stock list from PSE EDGE, candles from TradingView
# ---------------------------------------------------------------------------

_EDGE_HEADERS = {
    # EDGE sits behind Cloudflare and wants a real browser User-Agent.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
}


def get_pse_symbols() -> list[str]:
    """Every listing in PSE EDGE's public company directory (~283, one
    primary security per company, ETFs included). Paged 50 at a time; the
    page's own "[Total N]" marker is checked so a partial scrape is caught
    and treated as a failure (empty list) rather than silently shrinking the
    universe — scan.prune_unscanned then leaves PSE watches alone."""
    symbols, total, page = [], None, 1
    try:
        while True:
            resp = requests.get(config.PSE_EDGE_DIRECTORY_URL, params={"pageNo": page},
                                headers=_EDGE_HEADERS, timeout=30)
            resp.raise_for_status()
            html = resp.text
            if total is None:
                m = re.search(r"\[Total\s*(\d+)\]", html)
                total = int(m.group(1)) if m else None
            # Symbol column: the 2nd cmDetail(...) anchor in each row.
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
            found = 0
            for row in rows:
                anchors = re.findall(r"cmDetail\([^)]*\)[^>]*>\s*([^<]+?)\s*<", row)
                if len(anchors) >= 2:
                    symbols.append(anchors[1].strip())
                    found += 1
            if found == 0:
                break
            page += 1
            time.sleep(config.PSE_EDGE_RATE_LIMIT_SECONDS)
        if total is not None and len(symbols) != total:
            logger.error("PSE EDGE directory incomplete: got %d of %d — skipping PSE this run", len(symbols), total)
            return []
        return sorted(set(symbols))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load the PSE EDGE directory: %s", exc)
        return []


_tv = None


# 4H and 1W for PSE are built from 1H / 1D instead of fetched: halves the
# TradingView requests per stock, and with them the dropped connections
# (~1 in 9 on back-to-back requests) and their retries. Both are anchored to
# a Monday 09:30 Manila (01:30 UTC) origin, which reproduces TradingView's
# own PSE bars exactly: 4H = [09:30-13:30) + [13:30-close), weekly from
# Monday. (24h is a whole multiple of 4h, so the 4H anchor holds day to
# day. Weekly uses pandas' Monday-anchored "W-MON" bins — a "7D" rule
# ignores `origin` — then relabels to Monday 09:30 like TradingView's.)
_PSE_BUILT_FROM = {"4H": ("1H", "4h"), "1W": ("1D", "W-MON")}
_PSE_ORIGIN = pd.Timestamp("2024-01-01 01:30")  # a Monday, 09:30 Manila, naive UTC
_PSE_CACHE_SECONDS = 120
_pse_cache: dict = {}


def fetch_pse_ohlc(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """PSE candles for any timeframe, from at most two TradingView requests
    per stock (1H and 1D), shared briefly across calls — scan_asset asks for
    1H, 4H, 1D and 1W of the same stock back to back."""
    base_tf, rule = _PSE_BUILT_FROM.get(timeframe, (timeframe, None))
    now = time.time()
    for k in [k for k, (t, _) in _pse_cache.items() if now - t > _PSE_CACHE_SECONDS]:
        del _pse_cache[k]
    hit = _pse_cache.get((symbol, base_tf))
    df = hit[1] if hit else fetch_tradingview_ohlc(symbol, base_tf)
    if df is None:
        return None
    _pse_cache[(symbol, base_tf)] = (now, df)
    if rule is None:
        return df
    ohlcv = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if rule == "W-MON":
        out = df.resample(rule, label="left", closed="left").agg(ohlcv)
        out.index = out.index + pd.Timedelta(hours=1, minutes=30)
    else:
        out = df.resample(rule, origin=_PSE_ORIGIN).agg(ohlcv)
    return out.dropna(subset=["open", "high", "low", "close"])


def fetch_tradingview_ohlc(symbol: str, timeframe: str, exchange: str = config.PSE_TV_EXCHANGE) -> pd.DataFrame | None:
    """OHLC candles from TradingView via the unofficial tvdatafeed library
    (anonymous session — no login). Timestamps are converted to naive UTC,
    matching the Kraken frames: tvdatafeed builds them with
    datetime.fromtimestamp, i.e. in the *machine's* local timezone (UTC on
    GitHub's runners, Manila on a PH laptop)."""
    global _tv
    from datetime import datetime
    from tvDatafeed import Interval, TvDatafeed

    interval_name = config.TV_INTERVAL.get(timeframe)
    if interval_name is None:
        logger.error("No TradingView interval mapping for timeframe %s", timeframe)
        return None
    for attempt in range(3):
        try:
            if _tv is None:
                _tv = TvDatafeed()
            # tvdatafeed swallows its own websocket errors ("Connection to
            # remote host was lost") and just returns None — so an empty
            # result is retried too, on a fresh session.
            df = _tv.get_hist(symbol=symbol, exchange=exchange, interval=getattr(Interval, interval_name),
                               n_bars=config.TV_BARS_LONG_TF.get(timeframe, config.TV_BARS))
            if df is not None and not df.empty:
                local_tz = datetime.now().astimezone().tzinfo
                df.index = df.index.tz_localize(local_tz).tz_convert("UTC").tz_localize(None)
                return df[["open", "high", "low", "close", "volume"]].astype(float)
        except Exception as exc:  # noqa: BLE001
            logger.warning("TradingView fetch failed for %s@%s (attempt %d): %s", symbol, timeframe, attempt + 1, exc)
        _tv = None
        time.sleep(1.0 * (attempt + 1))
    logger.warning("TradingView: no data for %s@%s after retries", symbol, timeframe)
    return None


# ---------------------------------------------------------------------------
# Unified dispatcher
# ---------------------------------------------------------------------------

def fetch_ohlc(asset_class: str, symbol_or_ticker: str, timeframe: str) -> pd.DataFrame | None:
    """
    Single entry point scan.py should call, regardless of asset class.

    asset_class: one of "pse", "forex", "metals", "indices", "energy", "crypto"
    symbol_or_ticker: the yfinance ticker (forex/metals/indices/energy),
                       Kraken pair (crypto) or PSE symbol (pse) — NOT the
                       display name.
    """
    if asset_class == "crypto":
        return fetch_kraken_ohlc(symbol_or_ticker, timeframe)
    if asset_class == "pse":
        return fetch_pse_ohlc(symbol_or_ticker, timeframe)
    if asset_class in ("forex", "metals", "indices", "energy"):
        return fetch_yfinance_ohlc(symbol_or_ticker, timeframe)

    logger.error("Unknown asset class: %s", asset_class)
    return None
