"""
crypto_board.py — where crypto moved today, by category (crypto.html).

Each crypto coin has one category (crypto_categories.json, built by
make_crypto_categories.py). Every scan, from the 1H and 1D candles the
scan already fetched for each coin:

  - today's move: close / today's open - 1 (the day starts 00:00 UTC =
    8:00 AM Manila, Kraken's daily candle);
  - per category and for all crypto: the volume-weighted move (weight =
    the coin's previous-day USD volume, so bigger coins move it more),
    the median coin's move, and how many coins are up / down;
  - the same through the day, hour by hour (the intraday lines), and for
    the last HISTORY_DAYS days (the history heatmap);
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
OTHER = "Other"

_cats: dict | None = None
_bars: dict = {}     # ticker -> {"name", "cat", "large", "daily", "hourly"}


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


def record(asset: dict, df_1h: pd.DataFrame | None, df_1d: pd.DataFrame | None) -> None:
    """Keep what the board needs from one coin's candles (called from
    scan.scan_asset for every untagged crypto coin)."""
    if df_1d is None or len(df_1d) < 3:
        return
    daily = df_1d[["open", "close", "volume"]].iloc[-(HISTORY_DAYS + 2):].copy()
    daily.index = _utc_index(daily)
    hourly = None
    if df_1h is not None and len(df_1h):
        h = df_1h[["close"]].copy()
        h.index = _utc_index(h)
        hourly = h["close"][h.index >= daily.index[-1]]
    _bars[asset["ticker"]] = {"name": asset["display_name"], "cat": asset.get("category") or category_of(asset["display_name"]),
                              "large": bool(asset.get("large_cap")), "daily": daily, "hourly": hourly}


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
    # Days: today and the HISTORY_DAYS - 1 before it, each coin's day move
    # weighted by its previous day's USD volume.
    days = sorted({d for c in coins for d in c["daily"].index if d > today - pd.Timedelta(days=HISTORY_DAYS)})

    def day_move(c, d):
        daily = c["daily"]
        if d not in daily.index:
            return None
        i = daily.index.get_loc(d)
        row = daily.iloc[i]
        if not row["open"]:
            return None
        prev = daily.iloc[i - 1] if i > 0 else row
        return (row["close"] / row["open"] - 1, float(prev["close"] * prev["volume"]))

    history = [{"date": d.strftime("%Y-%m-%d"), **_group(coins, lambda c, d=d: day_move(c, d))} for d in days]

    # Intraday: each hour's close against today's open (a coin with no
    # trade that hour keeps its last move).
    hours = sorted({t for c in coins if c["hourly"] is not None for t in c["hourly"].index})
    series = {}
    for t in hours:
        def at(c, t=t):
            mv = day_move(c, today)
            h = c["hourly"]
            if mv is None or h is None:
                return None
            upto = h[h.index <= t]
            if not len(upto):
                return None
            return (float(upto.iloc[-1]) / float(c["daily"].loc[today, "open"]) - 1, mv[1])
        for k, s in _group(coins, at).items():
            if s:
                series.setdefault(k, {"vw": [], "med": []})
                series[k]["vw"].append(s["vw"]); series[k]["med"].append(s["med"])

    today_stats = _group(coins, lambda c: day_move(c, today))
    for k, s in today_stats.items():
        if s:
            w = watches.get(k, {})
            s["bull"], s["bear"] = w.get("BULLISH", 0), w.get("BEARISH", 0)
    return {"today": today_stats, "intraday": {"times": [t.isoformat() for t in hours], "series": series}, "days": history}


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
        "categories": order,
        "coins": [{"t": c["ticker"], "n": c["name"], "c": c["cat"], "l": c["large"], "chg": chg(c),
                   "w": coin_watches.get(c["ticker"], [])} for c in coins],
        "ref": {n: chg(c) for c in coins for n in ("BTC", "ETH") if c["name"] == f"{n}/USD"},
        "sets": sets,
    }
    Path(OUT_FILE).write_text(json.dumps(out, separators=(",", ":")))
    logger.info("Wrote %s: %d coins, %d categories.", OUT_FILE, len(coins), len(order))
