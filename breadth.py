"""
breadth.py — the 1H bias panel on the markets page (crypto.html).

Each hourly scan records one row per market from the 1H trend watchlist:
  u / d    watches on the 1H list now, bullish / bearish
           (the page shows u / (u + d) as "% bullish")
  nu / nd  watches that joined the list (or flipped direction) since the
           previous scan — the page sums these over the last 24h (crypto)
           or the last session (PSE) for the "new watches" bars
  p        the reference price then (BTC / the PSEi)

PSE only gets a row when the PSEi has a new 1H bar (trading hours); new PSE
watches seen in between are carried into that row. The history lives in
state.json (STATE_KEY) and is published as breadth.json. It starts from
breadth_seed.json: Oct 1 2026 onward, rebuilt by replaying the 1H watch
rules on past candles.
"""

import json
import logging
from pathlib import Path

import pandas as pd

import market_board

logger = logging.getLogger(__name__)

STATE_KEY = "__breadth__"
OUT_FILE = "breadth.json"
SEED_FILE = "breadth_seed.json"
KEEP_DAYS = 45
MARKETS = ("crypto", "pse")


def _is_1h(key: str, e: dict, market: str) -> bool:
    return (e.get("asset_class") == market and e.get("higher_tf") == "1H"
            and e.get("setup", "trend") == "trend" and not key.endswith(":sma50"))


def directions(state: dict) -> dict:
    """Watch key -> direction for every 1H trend watch (taken before the scan)."""
    return {k: e.get("direction") for k, e in state.items()
            if not k.startswith("__") and any(_is_1h(k, e, m) for m in MARKETS)}


def load(saved: dict | None) -> dict:
    if saved:
        return saved
    try:
        hist = json.loads(Path(SEED_FILE).read_text())
        logger.info("Breadth history started from %s.", SEED_FILE)
        return hist
    except (OSError, ValueError):
        return {m: [] for m in MARKETS}


def record(hist: dict, before: dict, state: dict) -> dict:
    """Add this scan's row per market; returns the history to save."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    cutoff = (now - pd.Timedelta(days=KEEP_DAYS)).isoformat()
    pending = hist.setdefault("pending", {})
    for m in MARKETS:
        up = dn = nu = nd = 0
        for k, e in state.items():
            if k.startswith("__") or not _is_1h(k, e, m):
                continue
            bull = e.get("direction") == "BULLISH"
            up, dn = up + bull, dn + (not bull)
            if before.get(k) != e.get("direction"):
                nu, nd = nu + bull, nd + (not bull)
        p0 = pending.get(m, [0, 0])
        nu, nd = nu + p0[0], nd + p0[1]
        ref = market_board.ref_price(m)
        rows = hist.setdefault(m, [])
        if m == "pse":
            bar = market_board.ref_bar(m)
            if bar is None or (rows and rows[-1].get("bar") == bar):
                pending[m] = [nu, nd]   # outside trading hours: carry to the next row
                continue
        row = {"t": now.isoformat(), "u": up, "d": dn, "nu": nu, "nd": nd, "p": ref}
        if m == "pse":
            row["bar"] = bar
        if rows and rows[-1]["t"] == row["t"]:   # a second scan this hour (a manual run)
            row["nu"] += rows[-1]["nu"]; row["nd"] += rows[-1]["nd"]
            rows[-1] = row
        else:
            rows.append(row)
        pending[m] = [0, 0]
        hist[m] = [r for r in rows if r["t"] >= cutoff]
    return hist


def write(hist: dict) -> None:
    out = {"generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
           **{m: [{k: v for k, v in r.items() if k != "bar"} for r in hist.get(m, [])] for m in MARKETS}}
    Path(OUT_FILE).write_text(json.dumps(out, separators=(",", ":")))
