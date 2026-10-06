"""
trades.py — forward test of the trades planned on the dashboard.

The dashboard writes trades.json (via the GitHub API): one trade = a basket
pick plus an entry, cut loss (CL) and target (TP) clicked on its chart.
Long when the CL is below the entry, short when above.

  pending -> open    a candle's range reaches the entry (a wick is enough,
                     either direction — no expiry; the user cancels plans)
  open    -> tp / cl a candle's high/low touches the TP or the CL

Checked on the finest timeframe that still covers the trade's time span
(5m, then 15m, 1H, 4H, 1D for older stretches), so when one candle on the
planned chart touches both levels the smaller timeframe shows which came
first. When even that candle touched both, it counts as CL — and a candle
that fills the entry can close it at CL, never at TP (the order inside it
is unknown).

Nothing is stored between runs except the transitions: every run replays
the candles since the plan was made (or since the fill), so missed runs
can't leave a trade in a wrong state. Transitions are written back with
the file's sha, only onto a trade still in the state this run saw (so a
cancel from the dashboard wins), and each one sends a Discord alert.

Called from basket.run_live (crypto, and PSE in session — every 5 min)
and from scan.run (all trades, hourly). scan.run also writes
trades_data.json: the charts the forward-test page shows for each trade.
"""

import base64
import json
import logging
import os
from pathlib import Path

import pandas as pd
import requests

import config
import fetch
import scan

logger = logging.getLogger(__name__)

TRADES_FILE = "trades.json"
TRADES_DATA_FILE = "trades_data.json"
TF_CHAIN = ["5m", "15m", "1H", "4H", "1D"]
ACTIVE = ("pending", "open")
# Closed trades keep their charts on the forward-test page this long.
CHART_DAYS_AFTER_CLOSE = 14


def load_trades() -> list:
    path = Path(TRADES_FILE)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("trades", [])
    except json.JSONDecodeError:
        logger.error("%s is corrupt — no trades checked this run.", TRADES_FILE)
        return []


def _utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def is_long(t: dict) -> bool:
    return t["entry"] > t["cl"]


def candle_stream(asset: dict, start: pd.Timestamp) -> list:
    """(open, end, high, low, tf) for every candle opening at or after
    `start`, on the finest timeframe covering each stretch: the recent part
    from 5m, anything older than the 5m history from the next timeframe
    that reaches back that far. Includes the still-forming candle."""
    stream, covered_from = [], None
    for tf in TF_CHAIN:
        df = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], tf)
        if df is None or df.empty:
            continue
        bar = pd.Timedelta(minutes=config.TIMEFRAME_MINUTES[tf])
        rows = []
        for t, h, l in zip(df.index, df["high"].to_numpy(dtype=float), df["low"].to_numpy(dtype=float)):
            t = _utc(t)
            if t < start or (covered_from is not None and t + bar > covered_from):
                continue
            rows.append((t, t + bar, h, l, tf))
        stream = rows + stream
        first = _utc(df.index[0])
        if first <= start:
            break
        covered_from = first if covered_from is None else min(covered_from, first)
    return stream


def evaluate(t: dict, stream: list) -> dict | None:
    """Replay a pending/open trade over its candles. Returns the fields to
    change (with "status"), or None if nothing happened."""
    long, entry, cl, tp = is_long(t), t["entry"], t["cl"], t["tp"]
    status = t["status"]
    fill_end = _utc(t["fill_end"]) if t.get("fill_end") else None
    out = {}
    for start, end, high, low, tf in stream:
        if status == "pending":
            if not (low <= entry <= high):
                continue
            status, fill_end = "open", end
            out.update(status="open", filled_at=start.isoformat(), fill_end=end.isoformat(), fill_tf=tf)
            if (low <= cl) if long else (high >= cl):
                out.update(_close(t, "cl", start, tf, f"filled and hit CL in the same {tf} candle"))
                return out
            continue
        # Open: the fill candle itself can only add a CL (order unknown).
        hit_cl = (low <= cl) if long else (high >= cl)
        hit_tp = ((high >= tp) if long else (low <= tp)) and start >= fill_end
        if hit_cl and hit_tp:
            out.update(_close(t, "cl", start, tf, f"TP and CL in the same {tf} candle — counted as CL"))
            return out
        if hit_cl or hit_tp:
            out.update(_close(t, "cl" if hit_cl else "tp", start, tf, None))
            return out
    return out or None


def _close(t: dict, kind: str, at: pd.Timestamp, tf: str, note: str | None) -> dict:
    ratio = abs(t["tp"] - t["entry"]) / abs(t["entry"] - t["cl"])
    return {"status": kind, "closed_at": at.isoformat(), "exit_tf": tf, "exit": t[kind],
            "result_r": round(ratio if kind == "tp" else -1.0, 2), "note": note}


def check(classes: set | None = None, pse_open: bool = True) -> None:
    """Check every pending/open trade (of `classes`, if given; PSE ones
    only if `pse_open`) and write back what changed."""
    trades = [t for t in load_trades() if t.get("status") in ACTIVE
              and (classes is None or t["asset_class"] in classes)
              and (t["asset_class"] != "pse" or pse_open)]
    updates = {}
    for t in trades:
        start = _utc(t["filled_at"] if t["status"] == "open" else t["created_at"])
        asset = {k: t[k] for k in ("asset_class", "ticker", "display_name")}
        try:
            change = evaluate(t, candle_stream(asset, start))
        except Exception as exc:  # noqa: BLE001
            logger.error("Trade %s (%s) check failed: %s", t["id"], t["display_name"], exc)
            continue
        if change:
            updates[t["id"]] = {"from": t["status"], **change}
            logger.info("TRADE %s %s: %s -> %s", t["display_name"], t["id"], t["status"], change["status"])
    if updates:
        send_alerts(write_updates(updates))


def write_updates(updates: dict) -> list:
    """Apply transitions to trades.json in the repo, each only if the trade
    is still in the state this run started from. Returns the updated trades
    (with their previous status) that were actually written."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        logger.info("GITHUB_TOKEN/GITHUB_REPOSITORY not set (local run) — not writing %d trade updates.", len(updates))
        return []
    url = f"https://api.github.com/repos/{repo}/contents/{TRADES_FILE}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            current = resp.json()
            trades = json.loads(base64.b64decode(current["content"])).get("trades", [])
            applied = []
            for tr in trades:
                u = updates.get(tr.get("id"))
                if u and tr.get("status") == u["from"]:
                    tr.update({k: v for k, v in u.items() if k != "from"})
                    applied.append({**tr, "from": u["from"]})
            if not applied:
                return []
            body = json.dumps({"trades": trades}, indent=2) + "\n"
            put = requests.put(url, headers=headers, timeout=15, json={
                "message": "Trades: " + ", ".join(f"{a['display_name']} {a['status']}" for a in applied),
                "content": base64.b64encode(body.encode()).decode(),
                "sha": current["sha"],
            })
            if put.status_code == 409:
                continue
            put.raise_for_status()
            return applied
        except Exception as exc:  # noqa: BLE001
            logger.error("Writing trade updates failed (attempt %d): %s", attempt + 1, exc)
    return []


def send_alerts(applied: list) -> None:
    """One embed per transition: FILLED, TP HIT, CL HIT (a fill that also
    hit CL sends just the CL)."""
    url = os.environ.get(config.DISCORD_ENTRY_WEBHOOK_ENV)
    if not applied or not url:
        return
    sent_at = pd.Timestamp.now(tz="UTC").isoformat()
    embeds = []
    for a in applied:
        side = "long" if is_long(a) else "short"
        name = f"{a['display_name']} {side} · {a['plan_tf']}"
        levels = f"entry `{scan._sig(a['entry'])}` · CL `{scan._sig(a['cl'])}` · TP `{scan._sig(a['tp'])}`"
        if a["status"] == "open":
            embeds.append({"title": f"FILLED · {name}", "color": 0xB8921C,
                           "description": f"{levels}\nFilled on the {a['fill_tf']} candle <t:{int(_utc(a['filled_at']).timestamp())}:t>",
                           "timestamp": sent_at})
        else:
            tp = a["status"] == "tp"
            how = a.get("note") or f"on the {a['exit_tf']} candle"
            embeds.append({"title": f"{'TP HIT' if tp else 'CL HIT'} · {name} · {a['result_r']:+.2f}R",
                           "color": 0x2F7A4F if tp else 0xA8402C,
                           "description": f"{levels}\n{how} <t:{int(_utc(a['closed_at']).timestamp())}:t>",
                           "timestamp": sent_at})
    for i in range(0, len(embeds), 10):
        try:
            requests.post(url, json={"embeds": embeds[i:i + 10]}, timeout=10).raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("Discord trade alert failed: %s", exc)


def write_charts() -> None:
    """trades_data.json: for each trade's pick (asset + higher tf), the same
    three charts as the dashboard — the higher tf and its two lower tfs."""
    now = pd.Timestamp.now(tz="UTC")
    keep = [t for t in load_trades() if t.get("status") in ACTIVE
            or (t.get("status") in ("tp", "cl") and now - _utc(t["closed_at"]) < pd.Timedelta(days=CHART_DAYS_AFTER_CLOSE))]
    charts = {}
    for t in keep:
        key = f"{t['asset_class']}:{t['ticker']}:{t['higher_tf']}"
        if key in charts:
            continue
        asset = {k: t[k] for k in ("asset_class", "ticker", "display_name")}
        entry = {}
        df = scan.fetch_and_compute(asset, t["higher_tf"], min_bars=60)
        if df is not None:
            entry[t["higher_tf"]] = scan.serialize_candles(df)
        for ltf in config.LOWER_TF_MAP[t["higher_tf"]]:
            d = scan.fetch_and_compute(asset, ltf, min_bars=60)
            if d is not None:
                entry[ltf] = scan.serialize_candles(d, n=scan.lower_tf_candle_count(t["higher_tf"], ltf))
        charts[key] = entry
    Path(TRADES_DATA_FILE).write_text(json.dumps({"generated_at": now.isoformat(), "charts": charts},
                                                 separators=(",", ":"), default=str))
    logger.info("Wrote %s with charts for %d picks.", TRADES_DATA_FILE, len(charts))
