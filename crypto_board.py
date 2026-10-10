"""
crypto_board.py — where crypto moved today, by category (crypto.html).

Each crypto coin has one category (crypto_categories.json, built by
make_crypto_categories.py). Every scan, from the 1H, 4H and 1D candles
the scan already fetched for each coin:

  - the move: the latest price against the close MOVE_4H_CANDLES 4H
    candles before the current one (~40 h), so a green day right after a
    new low still reads as a down move;
  - today's move: close / today's open - 1 (the day starts 00:00 UTC =
    8:00 AM Manila, Kraken's daily candle), shown beside it;
  - per category and for all crypto: the volume-weighted move (weight =
    the coin's median daily USD volume over the WEIGHT_DAYS before, so
    bigger coins move it more but one day's spike can't take over),
    the median coin's move, and how many coins are up / down;
  - the move hour by hour through today (the intraday lines);
  - for the last HISTORY_DAYS days (the history heatmap), each day's
    close against the close HISTORY_LOOKBACK_DAYS days before — the
    bigger picture;
  - the watchlist's bullish / bearish watches per category.

Everything is recomputed from candles each scan, so nothing has to be
stored — a lost cache can't lose history. Written to crypto.json for all
coins and for the large caps only.
"""

import json
import logging
import statistics
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CATS_FILE = "crypto_categories.json"
OUT_FILE = "crypto.json"
HISTORY_DAYS = 14
WEIGHT_DAYS = 14
MOVE_4H_CANDLES = 10
HISTORY_LOOKBACK_DAYS = 10
OTHER = "Other"

_cats: dict | None = None
_bars: dict = {}     # ticker -> {"name", "cat", "large", "daily", "hourly", "h4"}


def _load() -> dict:
    global _cats
    if _cats is None:
        try:
            _cats = json.loads(Path(CATS_FILE).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("%s missing or corrupt — every coin is %s.", CATS_FILE, OTHER)
            _cats = {"categories": [OTHER], "coins": {}}
    return _cats


def category_of(display_name: str) -> str:
    return _load()["coins"].get(display_name.split("/")[0], OTHER)


def _utc_index(df: pd.DataFrame) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(df.index)
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def record(asset: dict, df_1h: pd.DataFrame | None, df_4h: pd.DataFrame | None, df_1d: pd.DataFrame | None) -> None:
    """Keep what the board needs from one coin's candles (called from
    scan.scan_asset for every untagged crypto coin)."""
    if df_1d is None or len(df_1d) < 3:
        return
    daily = df_1d[["open", "close", "volume"]].iloc[-(HISTORY_DAYS + max(WEIGHT_DAYS, HISTORY_LOOKBACK_DAYS) + 1):].copy()
    daily.index = _utc_index(daily)
    hourly = None
    if df_1h is not None and len(df_1h):
        h = df_1h[["close"]].copy()
        h.index = _utc_index(h)
        hourly = h["close"][h.index >= daily.index[-1]]
    h4 = None
    if df_4h is not None and len(df_4h) > MOVE_4H_CANDLES:
        h4 = df_4h["close"].iloc[-(MOVE_4H_CANDLES + 8):].astype(float)
        h4.index = _utc_index(h4)
    _bars[asset["ticker"]] = {"name": asset["display_name"], "cat": asset.get("category") or category_of(asset["display_name"]),
                              "large": bool(asset.get("large_cap")), "daily": daily, "hourly": hourly, "h4": h4}


def _stats(items: list) -> dict | None:
    """items: (move, weight). Volume-weighted move, median move, up / down."""
    if not items:
        return None
    moves = [float(m) for m, _ in items]
    items = [(float(m), float(w)) for m, w in items]
    wsum = sum(w for _, w in items)
    vw = sum(m * w for m, w in items) / wsum if wsum > 0 else sum(moves) / len(moves)
    return {"vw": round(vw * 100, 2), "med": round(statistics.median(moves) * 100, 2),
            "up": int(sum(m > 0 for m in moves)), "down": int(sum(m < 0 for m in moves)), "n": len(moves)}


def _group(coins: list, value) -> dict:
    """Stats per category and "ALL" for one measure: value(coin) -> (move, weight) or None."""
    by = {}
    for c in coins:
        v = value(c)
        if v is None:
            continue
        by.setdefault(c["cat"], []).append(v)
        by.setdefault("ALL", []).append(v)
    return {k: _stats(v) for k, v in by.items()}


def _board(coins: list, watches: dict) -> dict:
    today = max(c["daily"].index[-1] for c in coins)
    # Days: today and the HISTORY_DAYS - 1 before it. day_move is the day's
    # own move (today's column on the board); day_trend is the close
    # against HISTORY_LOOKBACK_DAYS closes before (the heatmap). Both
    # weighted by the coin's median USD volume over the days before.
    days = sorted({d for c in coins for d in c["daily"].index if d > today - pd.Timedelta(days=HISTORY_DAYS)})

    def day_move(c, d):
        daily = c["daily"]
        if d not in daily.index:
            return None
        i = daily.index.get_loc(d)
        row = daily.iloc[i]
        if not row["open"]:
            return None
        return (row["close"] / row["open"] - 1, _weight(daily, i))

    def day_trend(c, d):
        daily = c["daily"]
        if d not in daily.index:
            return None
        i = daily.index.get_loc(d)
        if i < HISTORY_LOOKBACK_DAYS or not daily["close"].iloc[i - HISTORY_LOOKBACK_DAYS]:
            return None
        return (daily["close"].iloc[i] / daily["close"].iloc[i - HISTORY_LOOKBACK_DAYS] - 1, _weight(daily, i))

    history = [{"date": d.strftime("%Y-%m-%d"), **_group(coins, lambda c, d=d: day_trend(c, d))} for d in days]

    # The move: latest price against the close MOVE_4H_CANDLES 4H candles
    # before the current one (weighted like today's move).
    def move(c, price=None, at=None):
        h4, mv = c["h4"], day_move(c, today)
        if h4 is None or mv is None:
            return None
        i = len(h4) - 1 if at is None else int(h4.index.searchsorted(at, side="right")) - 1
        if i < MOVE_4H_CANDLES or not h4.iloc[i - MOVE_4H_CANDLES]:
            return None
        return ((h4.iloc[i] if price is None else price) / h4.iloc[i - MOVE_4H_CANDLES] - 1, mv[1])

    # Intraday: the move at each hour's close today (a coin with no trade
    # that hour keeps its last price).
    hours = sorted({t for c in coins if c["hourly"] is not None for t in c["hourly"].index})
    series = {}
    for t in hours:
        def at(c, t=t):
            h = c["hourly"]
            if h is None:
                return None
            upto = h[h.index <= t]
            if not len(upto):
                return None
            return move(c, float(upto.iloc[-1]), t)
        for k, s in _group(coins, at).items():
            if s:
                series.setdefault(k, {"vw": [], "med": []})
                series[k]["vw"].append(s["vw"]); series[k]["med"].append(s["med"])

    today_stats = _group(coins, lambda c: day_move(c, today))
    move_stats = _group(coins, move)
    for k, s in move_stats.items():
        if s:
            w = watches.get(k, {})
            s["bull"], s["bear"] = w.get("BULLISH", 0), w.get("BEARISH", 0)
    return {"move": move_stats, "today": today_stats,
            "intraday": {"times": [t.isoformat() for t in hours], "series": series}, "days": history}


def _weight(daily: pd.DataFrame, i: int) -> float:
    """Median daily USD volume over the WEIGHT_DAYS before row i."""
    before = daily.iloc[max(0, i - WEIGHT_DAYS):i] if i > 0 else daily.iloc[:1]
    return float((before["close"] * before["volume"]).median())


def coin_move(c: dict) -> float | None:
    """One coin's move in %, as on the board."""
    h4 = c["h4"]
    if h4 is None or len(h4) <= MOVE_4H_CANDLES or not h4.iloc[-1 - MOVE_4H_CANDLES]:
        return None
    return round(float(h4.iloc[-1] / h4.iloc[-1 - MOVE_4H_CANDLES] - 1) * 100, 2)


def build(state: dict) -> None:
    """Write crypto.json from the coins recorded this scan and the watch state."""
    if not _bars:
        logger.warning("No crypto candles recorded — %s not written.", OUT_FILE)
        return
    coin_watches = {}
    for e in state.values():
        if e.get("asset_class") == "crypto" and not e.get("tag"):
            coin_watches.setdefault(e["ticker"], []).append([e["higher_tf"], e["direction"], e.get("setup", "trend")])
    coins = list({**c, "ticker": t} for t, c in _bars.items())

    def watch_counts(subset):
        out = {}
        for c in subset:
            for _, d, _ in coin_watches.get(c["ticker"], []):
                for k in (c["cat"], "ALL"):
                    out.setdefault(k, {}).setdefault(d, 0)
                    out[k][d] += 1
        return out

    sets = {}
    for name, subset in (("all", coins), ("large", [c for c in coins if c["large"]])):
        if subset:
            sets[name] = _board(subset, watch_counts(subset))

    today = max(c["daily"].index[-1] for c in coins)
    def chg(c):
        d = c["daily"]
        return round((d["close"].iloc[-1] / d["open"].iloc[-1] - 1) * 100, 2) if d.index[-1] == today and d["open"].iloc[-1] else None

    order = [k for k in _load()["categories"] if any(c["cat"] == k for c in coins)]
    out = {
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "day_start": today.isoformat(),
        "move_candles": MOVE_4H_CANDLES,
        "history_lookback_days": HISTORY_LOOKBACK_DAYS,
        "categories": order,
        "coins": [{"t": c["ticker"], "n": c["name"], "c": c["cat"], "l": c["large"], "chg": chg(c), "m": coin_move(c),
                   "w": coin_watches.get(c["ticker"], [])} for c in coins],
        "ref": {n: chg(c) for c in coins for n in ("BTC", "ETH") if c["name"] == f"{n}/USD"},
        "ref_move": {n: coin_move(c) for c in coins for n in ("BTC", "ETH") if c["name"] == f"{n}/USD"},
        "sets": sets,
    }
    Path(OUT_FILE).write_text(json.dumps(out, separators=(",", ":")))
    logger.info("Wrote %s: %d coins, %d categories.", OUT_FILE, len(coins), len(order))
