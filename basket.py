"""
basket.py — lower-timeframe entry monitoring for the assets the user has
hand-picked from the watchlist ("the basket").

The dashboard writes basket.json (one pick = asset + higher timeframe +
direction) via the GitHub API. Each pick's two mapped lower timeframes
(config.LOWER_TF_MAP) are then watched independently for the entry setup:

  Bullish (bearish is the mirror image):
    1. Pullback — a FINISHED candle closes below the LSMA.
    2. MACD follows — from that pullback on, a FINISHED candle has a red
       (negative) MACD histogram. 1 + 2 together arm the setup.
    3. Entry — once armed, a candle trades above the LSMA (its high; a wick
       is enough, no close needed — so the still-forming candle counts)
       while that same candle's histogram is green. -> Discord alert.
    4. Re-arm — after an entry, a fresh pullback (1 + 2) is needed before
       that timeframe can alert again.

This is a deterministic replay over the same trailing window the dashboard's
lower-tf charts show (scan.lower_tf_candle_count), not a stored state
machine, so a lost cache can never leave a pick stuck in a wrong state.
The only persisted state is which candles already alerted (dedupe).

SMA 50 picks (setup "sma50", see scan.find_sma50_watch) use the same
pullback -> MACD -> break steps, run separately on the lower tf's SMA 50 and
its LSMA, from the higher-tf touch candle on — and each line fires only
once per pick. A candle crossing both lines sends one alert naming both.

A pick is removed from basket.json by the user, or automatically once its
higher-timeframe watch is gone (dropped off the watchlist, flipped
direction, or an SMA 50 watch's candles ran out) — deleted rather than
hidden, so a later re-trigger doesn't quietly bring it back.

Two entry points:
  - `python basket.py --live` — the 5-minute workflow (basket.yml): crypto
    picks (Kraken, real-time) and PSE picks during the PSE session
    (TradingView intraday). Yahoo's forex/metals/indices/energy data is
    delayed anyway, so those picks ride along with the hourly scan instead.
  - run_hourly(state) — called from scan.run(): removes picks whose watch
    dropped, checks the non-live picks for entries, and returns a status
    for every pick for the dashboard's basket view.
"""

import argparse
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

logger = logging.getLogger("basket")

# Dashboard-facing status per lower timeframe.
WAITING = "waiting"          # no pullback since the last entry (or window start)
PULLED_BACK = "pulled_back"  # closed past the LSMA, MACD hasn't followed yet
ARMED = "armed"              # pulled back + MACD followed — next LSMA break alerts
DONE = "done"                # SMA 50 picks: this line already fired its one entry


def is_sma50(pick: dict) -> bool:
    return pick.get("setup") == "sma50"


def pick_key(pick: dict) -> str:
    """Same as the pick's watch key in scan's state."""
    base = f"{pick['asset_class']}:{pick['ticker']}:{pick['higher_tf']}"
    return f"{base}:sma50" if is_sma50(pick) else base


# The lines each setup's entries break, and each line's alert-history key.
TREND_LINES = ("lsma",)
SMA50_LINES = ("sma50", "lsma")
LINE_LABEL = {"lsma": "LSMA", "sma50": "SMA 50"}


def dedupe_key(pick: dict, ltf: str, line: str) -> str:
    return f"{pick_key(pick)}|{ltf}|{line}" if is_sma50(pick) else f"{pick_key(pick)}|{ltf}"


# ---------------------------------------------------------------------------
# basket.json — read from the checkout, removals written back via the API
# ---------------------------------------------------------------------------

def load_basket() -> list:
    path = Path(config.BASKET_FILE)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("picks", [])
    except json.JSONDecodeError:
        logger.error("%s is corrupt — treating the basket as empty this run.", config.BASKET_FILE)
        return []


def remove_picks(keys: set, reasons: dict) -> None:
    """Delete picks from basket.json in the repo. Goes through the GitHub
    contents API with the file's current sha (not a git commit of the
    checkout), so it can't clobber a pick the user added from the dashboard
    while this run was going — a sha mismatch just refetches and retries."""
    if not keys:
        return
    for key in keys:
        logger.info("BASKET REMOVE: %s — %s", key, reasons.get(key, ""))

    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        logger.info("GITHUB_TOKEN/GITHUB_REPOSITORY not set (local run) — not writing removals back.")
        return

    url = f"https://api.github.com/repos/{repo}/contents/{config.BASKET_FILE}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            current = resp.json()
            picks = json.loads(base64.b64decode(current["content"])).get("picks", [])
            kept = [p for p in picks if pick_key(p) not in keys]
            if len(kept) == len(picks):
                return  # already gone (the user removed them first)
            body = json.dumps({"picks": kept}, indent=2) + "\n"
            put = requests.put(url, headers=headers, timeout=15, json={
                "message": f"Basket: auto-remove {', '.join(sorted(keys))}",
                "content": base64.b64encode(body.encode()).decode(),
                "sha": current["sha"],
            })
            if put.status_code == 409:
                continue  # someone else wrote in between — retry on the new version
            put.raise_for_status()
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("Writing basket removals failed (attempt %d): %s", attempt + 1, exc)
    logger.error("Gave up writing basket removals: %s", sorted(keys))


# ---------------------------------------------------------------------------
# Alert dedupe state — Actions cache, one file per workflow
# ---------------------------------------------------------------------------

def load_alerted(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_alerted(path: str, alerted: dict, live_keys: set) -> None:
    # Drop removed picks so the file doesn't grow forever.
    pruned = {k: v for k, v in alerted.items() if k.split("|")[0] in live_keys}
    Path(path).write_text(json.dumps(pruned, separators=(",", ":")))


# ---------------------------------------------------------------------------
# The entry setup — replayed over the lower-tf chart window
# ---------------------------------------------------------------------------

def _utc(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def evaluate_ltf(df: pd.DataFrame, ltf: str, direction: str, window: int, alerted_times: set,
                 line: str = "lsma", since: pd.Timestamp | None = None, once: bool = False) -> dict:
    """Replay the pullback -> MACD -> break setup against one line (the
    LSMA, or SMA 50) over the trailing `window` candles — from `since` on,
    if given, and stopping after the first entry if `once`. `alerted_times`
    (UTC iso strings) are candles that already fired an alert: they're
    treated as entries even if the candle later closed differently, so one
    pullback can never alert twice.

    Returns {"state": WAITING|PULLED_BACK|ARMED,
             "entries": [{"time": utc iso, "lsma": .., "extreme": ..}, ...],
             "events": [{"time": utc iso, "type": "pullback"|"armed"|"entry"}, ...]}
    — `events` is every step of every cycle, in order, for the dashboard's
    chart markers."""
    bullish = direction == config.DIRECTION_BULLISH
    last_closed = scan.last_closed_index(df, ltf)
    start = max(0, len(df) - window)

    pulled_back = macd_followed = False
    entries, events = [], []
    for i in range(start, len(df)):
        if since is not None and _utc(df.index[i]) < since:
            continue
        if once and entries:
            break
        row = df.iloc[i]
        lsma, hist = row[line], row["macd_hist"]
        if pd.isna(lsma) or pd.isna(hist):
            continue
        t = _utc(df.index[i]).isoformat()

        broke = (row["high"] > lsma) if bullish else (row["low"] < lsma)
        macd_ok = (hist > 0) if bullish else (hist < 0)
        if t in alerted_times or (pulled_back and macd_followed and broke and macd_ok):
            # `extreme` is what actually crossed the LSMA (the high/low — a
            # wick counts), which the close alone doesn't show.
            entries.append({"time": t, "lsma": float(lsma), "extreme": float(row["high"] if bullish else row["low"])})
            events.append({"time": t, "type": "entry"})
            pulled_back = macd_followed = False
            continue

        if i <= last_closed:  # arming only ever uses finished candles
            if not pulled_back and ((row["close"] < lsma) if bullish else (row["close"] > lsma)):
                pulled_back = True
                events.append({"time": t, "type": "pullback"})
            if pulled_back and not macd_followed and ((hist < 0) if bullish else (hist > 0)):
                macd_followed = True
                events.append({"time": t, "type": "armed"})

    if once and entries:
        state = DONE
    else:
        state = ARMED if pulled_back and macd_followed else PULLED_BACK if pulled_back else WAITING
    for e in events:
        e["line"] = line
    return {"state": state, "entries": entries, "events": events}


def check_pick(pick: dict, alerted: dict, now: pd.Timestamp, since: str | None = None) -> tuple[dict, list]:
    """Evaluate both lower timeframes of one pick. Returns (status, alerts):
    status per ltf for the dashboard, and new entries to alert on — only
    ones on candles still open when (or opened after) the pick was added,
    and within the last couple of candles, so a cold cache can't replay an
    old entry as a fresh alert. `since` is an SMA 50 pick's touch candle
    time: its lines are only replayed from there. Entries on the same
    candle (both lines at once) become one alert."""
    asset = {k: pick[k] for k in ("asset_class", "ticker", "display_name")}
    added_at = _utc(pd.Timestamp(pick.get("added_at") or now.isoformat()))
    # Lower tfs the user switched off on the dashboard: still evaluated (the
    # card and chart markers keep updating), just never alerted.
    muted = set(pick.get("muted") or [])
    sma50 = is_sma50(pick)
    lines = SMA50_LINES if sma50 else TREND_LINES
    since_ts = _utc(pd.Timestamp(since)) if (sma50 and since) else None
    status, alerts = {}, []

    for ltf in config.LOWER_TF_MAP[pick["higher_tf"]]:
        df = scan.fetch_and_compute(asset, ltf)
        if df is None:
            status[ltf] = {"state": "no_data", "last_entry": None}
            continue
        window = scan.lower_tf_candle_count(pick["higher_tf"], ltf)
        bar = pd.Timedelta(minutes=config.TIMEFRAME_MINUTES[ltf])
        recent_cutoff = now - max(2 * bar, pd.Timedelta(minutes=30))
        per_line, events, fresh = {}, [], {}
        for line in lines:
            done = set(alerted.get(dedupe_key(pick, ltf, line), []))
            result = evaluate_ltf(df, ltf, pick["direction"], window, done, line=line, since=since_ts, once=sma50)
            per_line[line] = {"state": result["state"], "last_entry": result["entries"][-1]["time"] if result["entries"] else None}
            events += result["events"]
            for e in result["entries"]:
                opened = pd.Timestamp(e["time"])
                if ltf in muted or e["time"] in done or opened + bar <= added_at or opened < recent_cutoff:
                    continue
                a = fresh.setdefault(e["time"], {"pick": pick, "ltf": ltf, "now": float(df.iloc[-1]["close"]),
                                                  "time": e["time"], "extreme": e["extreme"], "lines": []})
                a["lines"].append({"line": line, "level": e["lsma"]})
        alerts += fresh.values()
        if sma50:
            status[ltf] = {"lines": per_line, "events": sorted(events, key=lambda e: e["time"])}
        else:
            status[ltf] = {**per_line["lsma"], "events": events}
    return status, alerts


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def send_entry_alerts(alerts: list) -> bool:
    """One embed per entry, up to 10 per message (Discord's cap). Returns
    False if the webhook isn't configured or a send failed, so the caller
    doesn't mark those entries as alerted and retries next run."""
    if not alerts:
        return True
    url = os.environ.get(config.DISCORD_ENTRY_WEBHOOK_ENV)
    for a in alerts:
        logger.info("ENTRY: %s %s %s — %s broke %s @ %s", a["pick"]["display_name"], a["pick"]["direction"],
                    "SMA 50 setup" if is_sma50(a["pick"]) else "trend", a["ltf"],
                    " + ".join(LINE_LABEL[l["line"]] for l in a["lines"]), a["time"])
    if not url:
        logger.info("%s not set — skipping Discord send for %d entries.", config.DISCORD_ENTRY_WEBHOOK_ENV, len(alerts))
        return False

    sent_at = pd.Timestamp.now(tz="UTC").isoformat()
    embeds = []
    for a in alerts:
        p, bull = a["pick"], a["pick"]["direction"] == config.DIRECTION_BULLISH
        opened = int(pd.Timestamp(a["time"]).timestamp())
        tag = f" ({p['tag']})" if p.get("tag") else ""
        setup = " · SMA 50 setup" if is_sma50(p) else ""
        broke = " + ".join(f"{LINE_LABEL[l['line']]} `{scan._sig(l['level'])}`" for l in a["lines"])
        embeds.append({
            "title": f"Entry trigger: {p['display_name']}{tag} {'bullish' if bull else 'bearish'}{setup}",
            "color": 0x2F7A4F if bull else 0xA8402C,
            "description": (
                f"**{a['ltf']}** candle broke {'above' if bull else 'below'} {broke} · MACD {'green' if bull else 'red'}\n"
                f"Candle {'high' if bull else 'low'} `{scan._sig(a['extreme'])}` · price now `{scan._sig(a['now'])}`\n"
                # <t:…:t> renders in each reader's own timezone in Discord.
                f"{a['ltf']} candle opened <t:{opened}:t> · basket pick {p['higher_tf']} {'▲' if bull else '▼'}"
            ),
            "timestamp": sent_at,  # footer time = when this alert was sent
        })
    ok = True
    for i in range(0, len(embeds), 10):
        try:
            resp = requests.post(url, json={"embeds": embeds[i:i + 10]}, timeout=10)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error("Discord entry alert failed: %s", exc)
            ok = False
    return ok


def _alert_and_record(alerts: list, alerted: dict) -> None:
    """Send, and only on success remember the candles as alerted."""
    if alerts and send_entry_alerts(alerts):
        for a in alerts:
            for l in a["lines"]:
                alerted.setdefault(dedupe_key(a["pick"], a["ltf"], l["line"]), []).append(a["time"])


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

# Picks the 5-minute workflow owns (intraday data worth checking that often):
# crypto (Kraken, real-time) and PSE (TradingView, ~15 min delayed) — the
# latter only while the PSE session is open. The rest ride the hourly scan.
LIVE_CLASSES = {"crypto", "pse"}


def pse_session_open(now: pd.Timestamp) -> bool:
    """Mon-Fri within config.PSE_SESSION, Manila time. Holidays aren't
    known — a check on one just finds no new candles."""
    local = now.tz_convert(config.PSE_TIMEZONE)
    start, end = config.PSE_SESSION
    return local.weekday() < 5 and start <= local.strftime("%H:%M") <= end


def run_live() -> None:
    """5-minute workflow: crypto picks, plus PSE picks during the PSE
    session. Re-checks each pick's higher-tf watch fresh (same function the
    hourly scan uses) so a watch that broke is dropped within minutes, then
    checks the lower tfs."""
    now = pd.Timestamp.now(tz="UTC")
    picks = [p for p in load_basket()
             if p["asset_class"] == "crypto" or (p["asset_class"] == "pse" and pse_session_open(now))]
    alerted = load_alerted(config.BASKET_ALERTS_LIVE_FILE)
    remove, reasons, alerts = set(), {}, []
    # Current readable names/labels (crypto picks saved before the rename
    # carry Kraken codes like "XLTCZUSD") — one cheap call, only if needed.
    names = {p["pair"]: p for p in fetch.get_kraken_usd_pairs()} if any(p["asset_class"] == "crypto" for p in picks) else {}

    for pick in picks:
        if pick["ticker"] in names:
            pick = {**pick, "display_name": names[pick["ticker"]]["display_name"], "tag": names[pick["ticker"]]["tag"]}
        elif pick["asset_class"] == "pse":
            pick = {**pick, "tag": "PSE"}
        asset = {k: pick[k] for k in ("asset_class", "ticker", "display_name")}
        htf = pick["higher_tf"]
        auto_tf = config.AUTO_HIGHER_TF.get(htf)  # None for PSE 1W: no LSMA check
        df_htf = scan.fetch_and_compute(asset, htf, min_bars=config.MIN_BARS_BY_TF.get(htf, config.MIN_WARMUP_BARS))
        df_auto = scan.fetch_and_compute(asset, auto_tf, min_bars=config.LSMA_WARMUP_BARS) if auto_tf else None
        if df_htf is None or (auto_tf and df_auto is None):
            logger.warning("Fetch failed for %s — skipping this run", pick_key(pick))
            continue
        aligned = scan._align_auto_lsma(df_htf, df_auto) if auto_tf else None
        last_closed = scan.last_closed_index(df_htf, htf)
        if is_sma50(pick):
            watch = scan.find_sma50_watch(df_htf, aligned, last_closed)
            reason = _sma50_gone_reason(watch, pick)
        else:
            watch = scan.find_phase_a_watch(df_htf, aligned, last_closed)
            reason = _watch_gone_reason(watch, pick)
        if reason:
            remove.add(pick_key(pick)); reasons[pick_key(pick)] = reason
            continue
        since = watch["touch_at"].isoformat() if is_sma50(pick) else None
        _, new = check_pick(pick, alerted, now, since)
        alerts += new

    _alert_and_record(alerts, alerted)
    remove_picks(remove, reasons)
    # Keep history for every live-class pick still in the basket (not just
    # the ones checked this run — PSE picks are skipped out of session).
    live_keys = {pick_key(p) for p in load_basket() if p["asset_class"] in LIVE_CLASSES}
    save_alerted(config.BASKET_ALERTS_LIVE_FILE, alerted, live_keys - remove)


def _sma50_gone_reason(watch: dict | None, pick: dict) -> str | None:
    if watch is None:
        return f"SMA 50 watch ended ({config.SMA50_WATCH_CANDLES} candles since the touch)"
    if watch["direction"] != pick["direction"]:
        return f"SMA 50 watch flipped to {watch['direction']}"
    return None


def _watch_gone_reason(watch: dict | None, pick: dict) -> str | None:
    if watch is None:
        return "higher-tf watch dropped off (no trigger in the window)"
    if watch["invalidated_at"] is not None:
        return "higher-tf watch dropped off (closed past EMA20 after the last trigger)"
    if watch["direction"] != pick["direction"]:
        return f"higher-tf watch flipped to {watch['direction']}"
    return None


def run_hourly(state: dict) -> dict:
    """Called from scan.run() with the freshly updated watch state. Removes
    picks whose watch is gone, checks the non-live picks (forex, metals,
    indices, energy) for entries — crypto and PSE are the 5-minute
    workflow's job — and returns a status for every pick for data.json's
    basket view."""
    picks = load_basket()
    alerted = load_alerted(config.BASKET_ALERTS_HOURLY_FILE)
    # Live picks alert from the 5-minute workflow; its (read-only here)
    # history keeps their displayed status in step with what alerted.
    live_alerted = load_alerted(config.BASKET_ALERTS_LIVE_FILE)
    now = pd.Timestamp.now(tz="UTC")
    remove, reasons, alerts, statuses = set(), {}, [], {}

    for pick in picks:
        key = pick_key(pick)
        watch = state.get(key)
        if watch is None or watch.get("direction") != pick["direction"]:
            remove.add(key)
            gone = "SMA 50 watch ended" if is_sma50(pick) else "higher-tf watch dropped off"
            reasons[key] = gone if watch is None else f"higher-tf watch flipped to {watch['direction']}"
            continue
        pick = {**pick, "display_name": watch.get("display_name", pick["display_name"]), "tag": watch.get("tag")}
        is_live = pick["asset_class"] in LIVE_CLASSES
        status, new = check_pick(pick, live_alerted if is_live else alerted, now, watch.get("touch_at"))
        statuses[key] = status
        if not is_live:
            alerts += new

    _alert_and_record(alerts, alerted)
    remove_picks(remove, reasons)
    save_alerted(config.BASKET_ALERTS_HOURLY_FILE, alerted, {pick_key(p) for p in picks if p["asset_class"] not in LIVE_CLASSES} - remove)
    return statuses


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="5-minute run: crypto picks, plus PSE picks in session")
    args = parser.parse_args()
    if not args.live:
        parser.error("run with --live (the hourly basket check runs inside scan.py)")
    run_live()
