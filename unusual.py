"""
unusual.py — crypto coins trading far above their usual volume while
price runs up: an early look at moves like ZEUS's.

A coin is flagged on a closed 1H candle when

  - its USD volume is >= VOL_RATIO x its median hourly USD volume over
    the BASE_HOURS before (the "N x" shown everywhere),
  - price is up >= MOVE_4H over the last 4 hours (up moves only),
  - at least MIN_USD_HOUR traded in that hour (so one small trade in a
    thin coin can't do it — but a dormant coin waking up, like ZEUS at
    ~$100/h before its run, still counts),

at most once per coin every COOLDOWN_HOURS. Tested on 14 days of all
Kraken coins (Sep 27 - Oct 11): ~9 alerts a day; caught ZEUS on Oct 5
(595x volume, +467% more after the alert), STRK, DRV; 44 of 89 runs of
+40% caught, 20 with at least half the run still ahead. 35% of alerts saw
+10% within 72 h (21% for any coin), but the median alert is down ~4%
a day later: an early look, not an entry signal — many spikes fade.

Recomputed from the scan's 1H candles each run; only the last alert sent
per coin is kept (in state.json) so Discord gets each one once.
"""

import logging
import os

import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

VOL_RATIO = 20
MOVE_4H = 0.05
MIN_USD_HOUR = 50_000
BASE_HOURS = 168
COOLDOWN_HOURS = 72
# Flags shown on the dashboard / crypto page this long after the candle.
SHOW_HOURS = 24
# Only alert candles this recent (a first run mustn't send old ones).
ALERT_HOURS = 3
STATE_KEY = "__unusual__"

_flags: dict = {}    # ticker -> latest flag within SHOW_HOURS


def _utc(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx)
    return idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")


def events(df_1h: pd.DataFrame, now: pd.Timestamp) -> list:
    """Flags on the closed 1H candles of the last COOLDOWN_HOURS + SHOW_HOURS,
    replaying the cooldown from there."""
    if df_1h is None or len(df_1h) < BASE_HOURS // 2 + 5:
        return []
    df = df_1h[["close", "volume"]].astype(float).copy()
    df.index = _utc(df.index)
    df = df[df.index + pd.Timedelta(hours=1) <= now]          # closed candles only
    usd = df["close"] * df["volume"]
    base = usd.rolling(BASE_HOURS, min_periods=BASE_HOURS // 2).median().shift(1)
    mv4 = df["close"] / df["close"].shift(4) - 1
    since = now - pd.Timedelta(hours=COOLDOWN_HOURS + SHOW_HOURS)
    out, last = [], None
    for t in df.index[df.index >= since]:
        b, u, m = base.get(t), usd[t], mv4.get(t)
        if not (b and b > 0 and u >= MIN_USD_HOUR and m is not None and m >= MOVE_4H and u >= VOL_RATIO * b):
            continue
        if last is not None and t - last < pd.Timedelta(hours=COOLDOWN_HOURS):
            continue
        last = t
        out.append({"t": (t + pd.Timedelta(hours=1)).isoformat(), "x": round(u / b), "mv4": round(float(m) * 100, 1),
                    "usd": round(float(u)), "price": float(df["close"][t])})
    return out


def record(asset: dict, df_1h: pd.DataFrame | None) -> None:
    """Called from scan.scan_asset for every untagged crypto coin."""
    now = pd.Timestamp.now(tz="UTC")
    try:
        ev = [e for e in events(df_1h, now) if pd.Timestamp(e["t"]) >= now - pd.Timedelta(hours=SHOW_HOURS)]
    except Exception as exc:  # noqa: BLE001
        logger.error("Unusual-volume check failed for %s: %s", asset["display_name"], exc)
        return
    if ev:
        _flags[asset["ticker"]] = {**ev[-1], "ticker": asset["ticker"], "name": asset["display_name"],
                                   "cat": asset.get("category")}


def flags() -> list:
    """This run's flags, newest first."""
    return sorted(_flags.values(), key=lambda f: f["t"], reverse=True)


def tag_watches(state: dict) -> None:
    """Put the flag ({x, t}) on each crypto watch of a flagged coin (and take
    old ones off) for the dashboard's tag."""
    for key, e in state.items():
        if key.startswith("__"):
            continue
        f = _flags.get(e["ticker"]) if e.get("asset_class") == "crypto" else None
        if f:
            e["unusual"] = {"x": f["x"], "t": f["t"], "mv4": f["mv4"]}
        else:
            e.pop("unusual", None)


def send_alerts(state: dict, sent: dict) -> dict:
    """Discord alert for each new flag (candle within ALERT_HOURS, after the
    last one sent for that coin). Returns the updated sent map, pruned."""
    now = pd.Timestamp.now(tz="UTC")
    new = [f for f in flags() if pd.Timestamp(f["t"]) >= now - pd.Timedelta(hours=ALERT_HOURS)
           and f["t"] > sent.get(f["ticker"], "")]
    url = os.environ.get(config.DISCORD_ENTRY_WEBHOOK_ENV)
    if new and url:
        watches = {}
        for e in state.values():
            if isinstance(e, dict) and e.get("asset_class") == "crypto":
                watches.setdefault(e["ticker"], []).append(f"{e['higher_tf']}{'▲' if e['direction'] == 'BULLISH' else '▼'}")
        embeds = []
        for f in new:
            w = watches.get(f["ticker"])
            embeds.append({
                "title": f"UNUSUAL · {f['name']} ▲ +{f['mv4']}% in 4h",
                "color": 0x8A6FD1,
                "description": f"volume **{f['x']}×** usual (last hour ${f['usd']:,.0f})\n"
                               f"category {f['cat'] or '—'} · price `{f['price']:.6g}`\n"
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
        sent[f["ticker"]] = f["t"]
        logger.info("UNUSUAL: %s %sx volume, +%s%% in 4h", f["name"], f["x"], f["mv4"])
    cutoff = (now - pd.Timedelta(hours=COOLDOWN_HOURS + SHOW_HOURS)).isoformat()
    return {k: v for k, v in sent.items() if v >= cutoff}
