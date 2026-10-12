"""
unusual.py — crypto coins and PSE stocks trading far above their usual
volume while price runs up: an early look at moves like ZEUS's or ION's.

A name is flagged on a closed 1H candle when

  - its traded value (close x volume) is >= `ratio` x its usual hourly
    value (the "N x" shown everywhere),
  - price is up >= `move` over the last 4 candles (up moves only),
  - at least `min_value` traded in that hour (so one small trade in a
    thin name can't do it — but a dormant one waking up still counts),

at most once per name every `cooldown` candles. Per market (RULES):

  crypto  usual = median hourly USD value over the 168 hours before;
          20x, +5%, $50k; once per 72 h. Tested on 14 days of all Kraken
          coins (Sep 27 - Oct 11): ~9 a day; caught ZEUS on Oct 5 (595x,
          +467% more after the alert), STRK, DRV; 35% of alerts saw +10%
          within 72 h (21% for any coin), median ~-4% a day later.
  pse     usual = mean hourly peso value over the 110 candles (~20
          sessions) before — many PSE stocks have hours with no trades,
          so a median would often be 0; 5x, +5%, P1M; once per 18
          candles (~3 sessions). Tested on 60 days of all PSE stocks
          (Aug 13 - Oct 12): ~2 a trading day; 25 of 29 runs of +20%
          caught, 14 with at least half the run ahead (ION +184% at +17%
          from its low, TUGS, ACR, NRCP, AT, OPM); 42% of alerts saw +5%
          within 3 sessions (15% for any liquid stock), median -0.9% the
          next session.

An early look, not an entry signal — many spikes fade.

Recomputed from the scan's 1H candles each run; only the last alert sent
per name is kept (in state.json) so Discord gets each one once.
"""

import logging
import os

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

RULES = {
    "crypto": {"ratio": 20, "move": 0.05, "min_value": 50_000, "base": 168, "how": "median", "cooldown": 72, "cur": "$"},
    "pse": {"ratio": 5, "move": 0.05, "min_value": 1_000_000, "base": 110, "how": "mean", "cooldown": 18, "cur": "₱"},
}
# Flags shown on the dashboard / crypto page this long after the candle.
SHOW_HOURS = 24
# Only alert candles this recent (a first run mustn't send old ones).
ALERT_HOURS = 3
STATE_KEY = "__unusual__"

_flags: dict = {}    # (market, ticker) -> latest flag within SHOW_HOURS


def _utc(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def market_of(asset: dict) -> str | None:
    if asset.get("asset_class") == "pse":
        return "pse"
    if asset.get("asset_class") == "crypto" and not asset.get("tag"):
        return "crypto"
    return None


def events(df_1h: pd.DataFrame, now: pd.Timestamp, market: str = "crypto") -> list:
    """Flags on the closed 1H candles of the last cooldown + 48 candles,
    replaying the cooldown from there."""
    r = RULES[market]
    if df_1h is None or len(df_1h) < r["base"] // 2 + 5:
        return []
    df = df_1h[["close", "volume"]].astype(float).copy()
    df.index = _utc(df.index)
    df = df[df.index + pd.Timedelta(hours=1) <= now]          # closed candles only
    value = df["close"] * df["volume"]
    roll = value.rolling(r["base"], min_periods=r["base"] // 2)
    base = (roll.median() if r["how"] == "median" else roll.mean()).shift(1)
    mv4 = df["close"] / df["close"].shift(4) - 1
    out, last = [], None
    for i in range(max(0, len(df) - r["cooldown"] - 48), len(df)):
        b, u, m = base.iloc[i], value.iloc[i], mv4.iloc[i]
        if not (b > 0 and u >= r["min_value"] and m >= r["move"] and u >= r["ratio"] * b):
            continue  # (NaN comparisons are False)
        if last is not None and i - last < r["cooldown"]:
            continue
        last = i
        t = df.index[i]
        out.append({"t": (t + pd.Timedelta(hours=1)).isoformat(), "x": round(u / b), "mv4": round(float(m) * 100, 1),
                    "usd": round(float(u)), "price": float(df["close"].iloc[i])})
    return out


def record(asset: dict, df_1h: pd.DataFrame | None) -> None:
    """Called from scan.scan_asset for every untagged crypto coin and PSE stock."""
    market = market_of(asset)
    if market is None:
        return
    now = pd.Timestamp.now(tz="UTC")
    try:
        ev = [e for e in events(df_1h, now, market) if pd.Timestamp(e["t"]) >= now - pd.Timedelta(hours=SHOW_HOURS)]
    except Exception as exc:  # noqa: BLE001
        logger.error("Unusual-volume check failed for %s: %s", asset["display_name"], exc)
        return
    if ev:
        _flags[(market, asset["ticker"])] = {**ev[-1], "market": market, "ticker": asset["ticker"],
                                             "name": asset["display_name"], "cat": asset.get("category")}


def flags(market: str | None = None) -> list:
    """This run's flags (of one market), newest first."""
    return sorted((f for f in _flags.values() if market in (None, f["market"])), key=lambda f: f["t"], reverse=True)


def tag_watches(state: dict) -> None:
    """Put the flag ({x, t, mv4}) on each watch of a flagged name (and take
    old ones off) for the dashboard's tag."""
    for key, e in state.items():
        if key.startswith("__"):
            continue
        f = _flags.get((market_of(e), e["ticker"]))
        if f:
            e["unusual"] = {"x": f["x"], "t": f["t"], "mv4": f["mv4"]}
        else:
            e.pop("unusual", None)


def _sent_key(f: dict) -> str:
    # Crypto keeps its plain ticker (as first deployed); PSE is prefixed.
    return f["ticker"] if f["market"] == "crypto" else f"pse:{f['ticker']}"


def send_alerts(state: dict, sent: dict) -> dict:
    """Discord alert for each new flag (candle within ALERT_HOURS, after the
    last one sent for that name). Returns the updated sent map, pruned."""
    now = pd.Timestamp.now(tz="UTC")
    new = [f for f in flags() if pd.Timestamp(f["t"]) >= now - pd.Timedelta(hours=ALERT_HOURS)
           and f["t"] > sent.get(_sent_key(f), "")]
    url = os.environ.get(config.DISCORD_ENTRY_WEBHOOK_ENV)
    if new and url:
        watches = {}
        for e in state.values():
            if isinstance(e, dict) and market_of(e):
                watches.setdefault((market_of(e), e["ticker"]), []).append(f"{e['higher_tf']}{'▲' if e['direction'] == 'BULLISH' else '▼'}")
        embeds = []
        for f in new:
            w, cur = watches.get((f["market"], f["ticker"])), RULES[f["market"]]["cur"]
            embeds.append({
                "title": f"UNUSUAL · {f['name']}{' (PSE)' if f['market'] == 'pse' else ''} ▲ +{f['mv4']}% in 4h",
                "color": 0x8A6FD1,
                "description": f"volume **{f['x']}×** usual (last hour {cur}{f['usd']:,.0f})\n"
                               f"{'sector' if f['market'] == 'pse' else 'category'} {f['cat'] or '—'} · price `{f['price']:.6g}`\n"
                               + (f"watch: {' '.join(w)}" if w else "no watch yet"),
                "timestamp": now.isoformat(),
            })
        for i in range(0, len(embeds), 10):
            try:
                requests.post(url, json={"embeds": embeds[i:i + 10]}, timeout=10).raise_for_status()
            except Exception as exc:  # noqa: BLE001
                logger.error("Discord unusual-volume alert failed: %s", exc)
                return sent
    for f in new:
        sent[_sent_key(f)] = f["t"]
        logger.info("UNUSUAL: %s %sx volume, +%s%% in 4h", f["name"], f["x"], f["mv4"])
    cutoff = (now - pd.Timedelta(days=7)).isoformat()
    return {k: v for k, v in sent.items() if v >= cutoff}
