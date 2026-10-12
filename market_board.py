"""
market_board.py — where crypto and PSE moved, by category / sector
(crypto.html, its crypto and PSE tabs).

Each crypto coin has one category (crypto_categories.json, built by
make_crypto_categories.py); each PSE stock its PSE sector and subsector
(PSE EDGE's company directory, read with the universe). Every scan, from
the 1H, 4H and 1D candles the scan already fetched for each one:

  - the move: the latest price against the close MOVE_4H_CANDLES 4H
    candles before the current one (crypto ~6.7 days; PSE, with two 4H
    candles a session, ~20 trading days), so a green day right after a
    new low still reads as a down move;
  - today's move, shown beside it: crypto close / today's open - 1 (the
    day starts 00:00 UTC = 8:00 AM Manila, Kraken's daily candle); PSE
    close / the previous session's close - 1, as PSE quotes it;
  - per category and for the whole market: the volume-weighted move
    (weight = the median daily traded value over the WEIGHT_DAYS before,
    so bigger names move it more but one day's spike can't take over),
    the median move, and how many are up / down;
  - the move hour by hour through today (the intraday lines), and at
    each of the last HISTORY_4H_CANDLES 4H closes (the momentum line and
    the 4H tape, which shows the last 25);
  - for the last HISTORY_DAYS days / sessions (the daily tape), each day's
    close against the close HISTORY_LOOKBACK_DAYS days before — the
    bigger picture;
  - the watchlist's bullish / bearish watches per category.

Each market also has a reference row above the rest: BTC for crypto
(one of the coins), the PSEi for PSE (the index itself, fetched here, not
counted in any sector). PSE sectors also show PSE's official sector index
move beside ours.

Everything is recomputed from candles each scan, so nothing has to be
stored — a lost cache can't lose history. Written to crypto.json /
pse.json for everything and for the large caps (PSE: the PSEi 30) only.
"""

import json
import logging
import statistics
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

CATS_FILE = "crypto_categories.json"
HISTORY_DAYS = 25
WEIGHT_DAYS = 14
MOVE_4H_CANDLES = 40
HISTORY_4H_CANDLES = 40
HISTORY_LOOKBACK_DAYS = 10
OTHER = "Other"

MARKETS = {
    "crypto": {"out": "crypto.json", "today": "open", "ref_key": "BTC", "ref_label": "BTC", "all_label": "all crypto"},
    "pse": {"out": "pse.json", "today": "prev_close", "ref_key": "PSEI", "ref_label": "PSEi", "all_label": "all PSE"},
}
# BTC is a coin that also gets its own row.
BTC_NAME = "BTC/USD"
# PSE: the index and PSE's own sector indices (TradingView symbols).
PSEI_SYMBOL = "PSEI"
PSE_SECTOR_INDICES = {"Financials": "FIN", "Industrial": "IND", "Holding Firms": "HDG",
                      "Property": "PRO", "Services": "SVC", "Mining and Oil": "M_O"}
PSE_SECTOR_ORDER = list(PSE_SECTOR_INDICES)

_cats: dict | None = None
# market -> ticker -> {"name", "cat", "sub", "large", "member", "ref", "daily", "hourly", "h4"}
_bars: dict = {m: {} for m in MARKETS}


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


def _frames(df_1h, df_4h, df_1d) -> dict | None:
    if df_1d is None or len(df_1d) < 3:
        return None
    daily = df_1d[["open", "close", "volume"]].iloc[-(HISTORY_DAYS + max(WEIGHT_DAYS, HISTORY_LOOKBACK_DAYS) + 1):].astype(float)
    daily.index = _utc_index(daily)
    hourly = None
    if df_1h is not None and len(df_1h):
        h = df_1h["close"].astype(float)
        h.index = _utc_index(h)
        hourly = h[h.index >= daily.index[-1]]
    h4 = None
    if df_4h is not None and len(df_4h) > MOVE_4H_CANDLES:
        h4 = df_4h["close"].iloc[-(MOVE_4H_CANDLES + HISTORY_4H_CANDLES + 8):].astype(float)
        h4.index = _utc_index(h4)
    return {"daily": daily, "hourly": hourly, "h4": h4}


def record(asset: dict, df_1h: pd.DataFrame | None, df_4h: pd.DataFrame | None, df_1d: pd.DataFrame | None) -> None:
    """Keep what the board needs from one coin's / stock's candles (called
    from scan.scan_asset for every untagged crypto coin and PSE stock)."""
    market = "pse" if asset["asset_class"] == "pse" else "crypto"
    if market == "crypto":
        cat = asset.get("category") or category_of(asset["display_name"])
        if cat in _load().get("hidden", []):
            return  # stablecoins aren't on the board
    else:
        cat = asset.get("category") or OTHER
    f = _frames(df_1h, df_4h, df_1d)
    if f is None:
        return
    _bars[market][asset["ticker"]] = {"name": asset["display_name"], "cat": cat, "sub": asset.get("subsector"),
                                      "large": bool(asset.get("large_cap")), "member": True,
                                      "ref": "BTC" if asset["display_name"] == BTC_NAME else None, **f}


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
    """Stats per category, "ALL" and the reference row for one measure:
    value(coin) -> (move, weight) or None."""
    by = {}
    for c in coins:
        v = value(c)
        if v is None:
            continue
        if c["member"]:
            by.setdefault(c["cat"], []).append(v)
            by.setdefault("ALL", []).append(v)
        if c["ref"]:
            by.setdefault(c["ref"], []).append(v)
    return {k: _stats(v) for k, v in by.items()}


def _weight(daily: pd.DataFrame, i: int) -> float:
    """Median daily traded value over the WEIGHT_DAYS before row i."""
    before = daily.iloc[max(0, i - WEIGHT_DAYS):i] if i > 0 else daily.iloc[:1]
    return float((before["close"] * before["volume"]).median())


def _day_move(c: dict, d, mode: str):
    """(move, weight) of day d: against its open (crypto) or the previous
    session's close (PSE)."""
    daily = c["daily"]
    if d not in daily.index:
        return None
    i = daily.index.get_loc(d)
    ref = daily["open"].iloc[i] if mode == "open" else (daily["close"].iloc[i - 1] if i > 0 else 0)
    if not ref:
        return None
    return (daily["close"].iloc[i] / ref - 1, _weight(daily, i))


def _move_at(c: dict, price=None, at=None):
    """The move as a fraction: price (default: that 4H candle's close)
    against the close MOVE_4H_CANDLES 4H candles before the candle at `at`
    (default: the latest)."""
    h4 = c["h4"]
    if h4 is None:
        return None
    i = len(h4) - 1 if at is None else int(h4.index.searchsorted(at, side="right")) - 1
    if i < MOVE_4H_CANDLES or not h4.iloc[i - MOVE_4H_CANDLES]:
        return None
    return (h4.iloc[i] if price is None else price) / h4.iloc[i - MOVE_4H_CANDLES] - 1


def _board(coins: list, watches: dict, mode: str) -> dict:
    today = max(c["daily"].index[-1] for c in coins if c["member"])
    # The last HISTORY_DAYS days with candles (PSE: sessions, not weekends).
    days = sorted({d for c in coins if c["member"] for d in c["daily"].index if d <= today})[-HISTORY_DAYS:]
    # Weight of each name for the moves: its median traded value before its
    # latest session.
    weight = {id(c): _weight(c["daily"], len(c["daily"]) - 1) for c in coins}

    def day_trend(c, d):
        daily = c["daily"]
        if d not in daily.index:
            return None
        i = daily.index.get_loc(d)
        if i < HISTORY_LOOKBACK_DAYS or not daily["close"].iloc[i - HISTORY_LOOKBACK_DAYS]:
            return None
        return (daily["close"].iloc[i] / daily["close"].iloc[i - HISTORY_LOOKBACK_DAYS] - 1, _weight(daily, i))

    history = [{"date": d.strftime("%Y-%m-%d"), **_group(coins, lambda c, d=d: day_trend(c, d))} for d in days]

    def move(c, price=None, at=None):
        m = _move_at(c, price, at)
        return None if m is None else (m, weight[id(c)])

    # 4H heatmap: the move at each of the last HISTORY_4H_CANDLES 4H
    # candles (the last one still forming).
    bars = sorted({t for c in coins if c["member"] and c["h4"] is not None for t in c["h4"].index})[-HISTORY_4H_CANDLES:]
    history_4h = [{"time": t.isoformat(), **_group(coins, lambda c, t=t: move(c, at=t))} for t in bars]

    # Intraday: the move at each hour's close today (a name with no trade
    # that hour keeps its last price).
    hours = sorted({t for c in coins if c["member"] and c["hourly"] is not None for t in c["hourly"].index})
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

    today_stats = _group(coins, lambda c: _day_move(c, today, mode))
    move_stats = _group(coins, move)
    for k, s in move_stats.items():
        if s:
            w = watches.get(k, {})
            s["bull"], s["bear"] = w.get("BULLISH", 0), w.get("BEARISH", 0)
    return {"move": move_stats, "today": today_stats,
            "intraday": {"times": [t.isoformat() for t in hours], "series": series}, "days": history, "bars_4h": history_4h}


def _pct(x) -> float | None:
    return None if x is None else round(float(x) * 100, 2)


def _today_chg(c: dict, today, mode: str) -> float | None:
    mv = _day_move(c, today, mode) if c["daily"].index[-1] == today else None
    return _pct(mv[0]) if mv else None


def _pse_extras(today) -> tuple:
    """The PSEi (a reference "coin") and the official sector indices'
    move / today, fetched now (a few requests)."""
    import fetch  # deferred: fetch is heavy and only PSE needs it here
    def frames(symbol, tfs):
        return _frames(*(fetch.fetch_pse_ohlc(symbol, tf) if tf in tfs else None for tf in ("1H", "4H", "1D")))
    psei, sectors = None, {}
    try:
        f = frames(PSEI_SYMBOL, ("1H", "4H", "1D"))
        if f:
            psei = {"name": "PSEi", "cat": None, "sub": None, "large": True, "member": False, "ref": "PSEI", **f}
    except Exception as exc:  # noqa: BLE001
        logger.error("PSEi candles failed: %s", exc)
    for sector, sym in PSE_SECTOR_INDICES.items():
        try:
            f = frames(sym, ("4H", "1D"))
            if f:
                c = {"member": False, "ref": None, **f}
                m = _move_at(c)
                sectors[sector] = {"m": _pct(m), "chg": _today_chg(c, today, "prev_close")}
        except Exception as exc:  # noqa: BLE001
            logger.error("PSE %s index candles failed: %s", sector, exc)
    return psei, sectors


def build(market: str, state: dict, unusual: list | None = None) -> None:
    """Write the market's board file from the names recorded this scan, the
    watch state and (crypto) the unusual-volume flags (unusual.py)."""
    cfg, recorded = MARKETS[market], _bars[market]
    if not recorded:
        logger.warning("No %s candles recorded — %s not written.", market, cfg["out"])
        return
    mode = cfg["today"]
    coin_watches = {}
    for e in state.values():
        if e.get("asset_class") == market and (market != "crypto" or not e.get("tag")):
            coin_watches.setdefault(e["ticker"], []).append([e["higher_tf"], e["direction"], e.get("setup", "trend")])
    coins = [{**c, "ticker": t} for t, c in recorded.items()]
    today = max(c["daily"].index[-1] for c in coins)
    sector_index = {}
    if market == "pse":
        psei, sector_index = _pse_extras(today)
        if psei:
            coins.append({**psei, "ticker": PSEI_SYMBOL})

    def watch_counts(subset):
        out = {}
        for c in subset:
            for _, d, _ in coin_watches.get(c["ticker"], []):
                for k in ((c["cat"], "ALL") if c["member"] else ()) + ((c["ref"],) if c["ref"] else ()):
                    out.setdefault(k, {}).setdefault(d, 0)
                    out[k][d] += 1
        return out

    sets = {}
    for name, subset in (("all", coins), ("large", [c for c in coins if c["large"]])):
        if any(c["member"] for c in subset):
            sets[name] = _board(subset, watch_counts(subset), mode)

    if market == "crypto":
        order = [k for k in _load()["categories"] if any(c["cat"] == k for c in coins)]
    else:
        present = {c["cat"] for c in coins if c["member"]}
        order = [k for k in PSE_SECTOR_ORDER if k in present] + sorted(present - set(PSE_SECTOR_ORDER))
    members = [c for c in coins if c["member"]]
    refs = [("BTC", "BTC/USD"), ("ETH", "ETH/USD")] if market == "crypto" else [("PSEi", "PSEi")]
    by_name = {c["name"]: c for c in coins}
    out = {
        "market": market,
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "day_start": today.isoformat(),
        "move_candles": MOVE_4H_CANDLES,
        "history_lookback_days": HISTORY_LOOKBACK_DAYS,
        "ref_key": cfg["ref_key"], "ref_label": cfg["ref_label"], "all_label": cfg["all_label"],
        "categories": order,
        "coins": [{"t": c["ticker"], "n": c["name"], "c": c["cat"], "s": c["sub"], "l": c["large"],
                   "chg": _today_chg(c, today, mode), "m": _pct(_move_at(c)),
                   "w": coin_watches.get(c["ticker"], [])} for c in members],
        "ref": {k: _today_chg(by_name[n], today, mode) for k, n in refs if n in by_name},
        "ref_move": {k: _pct(_move_at(by_name[n])) for k, n in refs if n in by_name},
        "ref_level": round(float(by_name["PSEi"]["daily"]["close"].iloc[-1]), 2) if "PSEi" in by_name else None,
        "sector_index": sector_index,
        "sets": sets,
        "unusual": unusual or [],
    }
    Path(cfg["out"]).write_text(json.dumps(out, separators=(",", ":")))
    logger.info("Wrote %s: %d names, %d categories.", cfg["out"], len(members), len(order))
