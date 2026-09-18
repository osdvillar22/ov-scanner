"""
backtest.py — historical win-rate test of the RSI-extreme + LSMA/MACD
pullback-entry strategy that scan.py runs live.

Replays the exact same trigger/entry rules bar-by-bar over historical
data (instead of just the latest bar) and simulates stop-loss/take-profit
fills, so you can see how the strategy would have performed.

Rules locked in for this version:
  - Higher-tf bias (BULLISH/BEARISH) works exactly like scan.py's
    WATCHING state: starts when RSI clears 70/30, stays active for
    VISIBILITY_WINDOW_CANDLES higher-tf bars, then expires. A fresh
    extreme on the very bar an old one expires immediately restarts it.
  - Lower-tf state machine (PULLBACK -> CONVERGING -> TRIGGERED) is the
    same on_wrong_side / macd_confirmed / macd_close logic as
    scan.update_lower_tf_state, replayed bar-by-bar.
  - Stop-loss = the lowest low (BUY) / highest high (SELL) across every
    bar from the first PULLBACK bar through the TRIGGERED bar — the
    pullback's own extreme, not a generic swing detector.
  - Target = 1R (take-profit distance == stop distance).
  - Entry fills on the first bar after TRIGGERED whose high/low reaches
    the stop-order price; if it doesn't fill within FILL_TIMEOUT_BARS,
    the setup is discarded (not counted as a trade).
  - A candle that touches both stop and target in the same bar is
    scored as a LOSS (conservative — no intrabar data to know which hit
    first).
  - A trade that hits neither target nor stop within MAX_HOLD_BARS is
    reported separately as UNRESOLVED, not forced into a win/loss.

No lookahead: the higher-tf bias attached to each lower-tf bar only ever
uses higher-tf bars that had *already closed* at or before that lower-tf
bar's timestamp (via a backward merge_asof).
"""

import logging
from dataclasses import dataclass, field

import pandas as pd

import config
import fetch
import indicators

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest")

FILL_TIMEOUT_BARS = 10
MAX_HOLD_BARS = 200


def to_utc_iso(ts: pd.Timestamp) -> str:
    """A candle's own timestamp, UTC-normalized — same convention
    scan.serialize_candles uses, so a marker built from this lines up
    exactly with that candle's `time` on the dashboard's chart."""
    ts_utc = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts_utc.isoformat()


@dataclass
class Trade:
    asset_class: str
    ticker: str
    display_name: str
    higher_tf: str
    entry_tf: str
    direction: str
    trigger_time: object
    entry_time: object
    entry_price: float
    stop_price: float
    target_price: float
    outcome: str  # "WIN", "LOSS", "UNRESOLVED"


@dataclass
class Setup:
    """A TRIGGERED setup waiting to see if its stop order fills."""
    trigger_idx: int
    direction: str
    entry_price: float
    stop_loss_price: float
    target_price: float


def htf_bias_series(df_htf: pd.DataFrame) -> pd.DataFrame:
    """
    Bar-by-bar active direction bias, replaying scan.py's WATCHING window
    exactly (expiry after VISIBILITY_WINDOW_CANDLES, immediate re-arm on a
    fresh extreme the same bar an old one expires).

    A fresh RSI extreme ALWAYS re-anchors trigger_idx/age to itself, even if
    a window is already active in the same direction — the lower-tf entry
    setup should only ever track the pullback since the LATEST extreme, not
    a stale earlier one from the same window (this is what makes the
    strategy a momentum strategy, per the user's explicit rule).

    Returns a DataFrame (indexed like df_htf) with:
      bias         - active direction or None
      age          - candles since the current anchor's trigger bar
      trigger_idx  - integer position of that trigger bar
      trigger_rsi  - RSI value at that trigger bar
    trigger_idx/trigger_rsi let a caller find the *real* origin of the
    currently-active window, not just "now" — used to backfill a
    newly-discovered entry's true history instead of cold-starting it.
    """
    rsi = df_htf["rsi"]
    n = len(df_htf)
    bias = [None] * n
    age = [None] * n
    trigger_idx_col = [None] * n
    trigger_rsi_col = [None] * n
    active_direction = None
    candles_since_trigger = 0
    trigger_idx = None
    for i in range(n):
        r = rsi.iloc[i]
        if pd.isna(r):
            continue
        is_ob = r > config.RSI_OVERBOUGHT
        is_os = r < config.RSI_OVERSOLD

        if active_direction is not None:
            candles_since_trigger += 1
            if candles_since_trigger > config.VISIBILITY_WINDOW_CANDLES:
                active_direction, candles_since_trigger, trigger_idx = None, 0, None

        fresh_extreme = config.DIRECTION_BULLISH if is_ob else (config.DIRECTION_BEARISH if is_os else None)
        if fresh_extreme is not None:
            active_direction, candles_since_trigger, trigger_idx = fresh_extreme, 0, i

        bias[i] = active_direction
        if active_direction is not None:
            age[i] = candles_since_trigger
            trigger_idx_col[i] = trigger_idx
            trigger_rsi_col[i] = float(rsi.iloc[trigger_idx])
    return pd.DataFrame(
        {"bias": bias, "age": age, "trigger_idx": trigger_idx_col, "trigger_rsi": trigger_rsi_col},
        index=df_htf.index,
    )


def htf_trigger_origin(df_htf: pd.DataFrame) -> dict | None:
    """The real origin of the higher-tf window active as of the latest bar,
    or None if no window is currently active. Used by scan.py to backfill a
    newly-created entry's htf_candle_count/rsi_at_trigger/first_seen from
    already-fetched history instead of always starting at "now"."""
    info = htf_bias_series(df_htf)
    last = info.iloc[-1]
    # pd.isna(), not `is None`: a "bias" column mixing strings and None gets
    # silently coerced (pandas 3.x string dtype turns None into float nan on
    # element access), so `is None` would never actually match here.
    if pd.isna(last["bias"]):
        return None
    return {
        "direction": last["bias"],
        "htf_candle_count": int(last["age"]),
        "rsi_at_trigger": round(float(last["trigger_rsi"]), 2),
        "trigger_time": df_htf.index[int(last["trigger_idx"])],
    }


def replay_lower_tf(df_ltf: pd.DataFrame, bias: pd.DataFrame, asset: dict, higher_tf: str, entry_tf: str):
    """Walk the lower-tf bars in order, replaying PULLBACK/CONVERGING/TRIGGERED,
    simulate fill + win/loss/unresolved for every setup it produces, and track
    the state as of the final bar.

    `bias` is a DataFrame (indexed like df_ltf) with columns:
      bias    - active direction (BULLISH/BEARISH) or None
      anchor  - an opaque identity for the CURRENT anchor bar (its higher-tf
                trigger timestamp). Needed alongside `bias` because a fresh
                RSI extreme can re-anchor the window to itself without the
                *direction* changing (see htf_bias_series) — the lower-tf
                machine has to reset in that case too, not just on a
                direction flip.

    A TRIGGERED setup that hasn't filled yet gets cancelled — dropped back
    to WATCHING to immediately re-track a fresh pullback under the same
    still-active bias — if either: price makes a new swing past the
    setup's own risk stop-loss before it ever fills, or it simply times
    out (FILL_TIMEOUT_BARS). Neither counts as a trade.

    Returns (trades, current_state) — current_state is a dict matching
    scan.py's ltf_state schema (state/price/lsma/macd_gap_pct[/stop_price/
    stop_type/risk_stop_price/target_price/trigger_time/filled/filled_time]),
    used to backfill a freshly-discovered entry's real state instead of
    cold-starting it at WATCHING."""
    trades: list[Trade] = []
    state = config.STATE_WATCHING
    direction = None
    anchor = None
    pullback_low = None
    pullback_high = None
    pending_setup: Setup | None = None
    fill_wait = 0
    filled = False
    last_stop_price = None
    last_stop_type = None
    last_risk_stop_price = None
    last_target_price = None
    last_trigger_time = None
    last_filled_time = None
    last_macd_gap_pct = None

    n = len(df_ltf)
    for i in range(n):
        row = df_ltf.iloc[i]
        bar_bias = bias["bias"].iloc[i]
        bar_anchor = bias["anchor"].iloc[i]
        close, high, low = row["close"], row["high"], row["low"]
        lsma, macd, macd_signal, macd_hist = row["lsma"], row["macd"], row["macd_signal"], row["macd_hist"]

        # Handle a pending (TRIGGERED but not yet filled) setup first: fill,
        # price-based invalidation, or timeout. Any discard here drops
        # straight back to WATCHING and falls through to re-evaluate this
        # same bar fresh, instead of staying stuck sticky at TRIGGERED.
        if pending_setup is not None:
            fill_wait += 1
            hit = (high >= pending_setup.entry_price) if pending_setup.direction == config.DIRECTION_BULLISH \
                else (low <= pending_setup.entry_price)
            if hit:
                outcome = simulate_outcome(df_ltf, i, pending_setup)
                trades.append(Trade(
                    asset_class=asset["asset_class"], ticker=asset["ticker"], display_name=asset["display_name"],
                    higher_tf=higher_tf, entry_tf=entry_tf, direction=pending_setup.direction,
                    trigger_time=df_ltf.index[pending_setup.trigger_idx], entry_time=df_ltf.index[i],
                    entry_price=pending_setup.entry_price, stop_price=pending_setup.stop_loss_price,
                    target_price=pending_setup.target_price, outcome=outcome,
                ))
                pending_setup = None
                filled = True
                last_filled_time = df_ltf.index[i]
            else:
                invalidated = (low < pending_setup.stop_loss_price) if pending_setup.direction == config.DIRECTION_BULLISH \
                    else (high > pending_setup.stop_loss_price)
                if invalidated or fill_wait > FILL_TIMEOUT_BARS:
                    pending_setup = None
                    state = config.STATE_WATCHING
                    pullback_low = pullback_high = None

        # pd.isna(), not `is None` — see htf_trigger_origin's note: a "bias"
        # column mixing strings and None gets coerced under pandas 3.x, so
        # `bar_bias is None` would never match an inactive bar here.
        if pd.isna(bar_bias) or pd.isna(lsma) or pd.isna(macd):
            state = config.STATE_WATCHING
            direction = None
            anchor = None
            continue

        if bar_bias != direction or bar_anchor != anchor:
            # Bias/anchor changed (new trigger, flip, re-anchor to a fresher
            # extreme, or expiry) — reset the lower-tf machine.
            direction = bar_bias
            anchor = bar_anchor
            state = config.STATE_WATCHING
            pullback_low = pullback_high = None
            filled = False

        if direction == config.DIRECTION_BULLISH:
            on_wrong_side = close < lsma
            macd_confirmed = macd_hist > 0
        else:
            on_wrong_side = close > lsma
            macd_confirmed = macd_hist < 0

        macd_gap_pct = abs(macd - macd_signal) / abs(macd) if macd != 0 else float("inf")
        macd_close = macd_gap_pct <= config.MACD_CLOSENESS_PCT
        last_macd_gap_pct = macd_gap_pct

        prev_state = state
        if prev_state == config.STATE_TRIGGERED:
            new_state = config.STATE_TRIGGERED
        elif on_wrong_side:
            new_state = config.STATE_CONVERGING if macd_close else config.STATE_PULLBACK
        else:
            if prev_state in (config.STATE_PULLBACK, config.STATE_CONVERGING) and (macd_confirmed or macd_close):
                new_state = config.STATE_TRIGGERED
            else:
                new_state = config.STATE_WATCHING

        if new_state in (config.STATE_PULLBACK, config.STATE_CONVERGING):
            pullback_low = low if pullback_low is None else min(pullback_low, low)
            pullback_high = high if pullback_high is None else max(pullback_high, high)

        fresh_trigger = new_state == config.STATE_TRIGGERED and prev_state != config.STATE_TRIGGERED
        if fresh_trigger and pullback_low is not None and pending_setup is None:
            entry_price = float(high) if direction == config.DIRECTION_BULLISH else float(low)
            stop_loss_price = float(pullback_low) if direction == config.DIRECTION_BULLISH else float(pullback_high)
            risk = abs(entry_price - stop_loss_price)
            if risk > 0:
                target_price = entry_price + risk if direction == config.DIRECTION_BULLISH else entry_price - risk
                pending_setup = Setup(i, direction, entry_price, stop_loss_price, target_price)
                fill_wait = 0
                filled = False
                # scan.py's ltf_state["stop_price"] means the entry buy/sell-stop
                # order price — same thing as Setup.entry_price here, not the
                # risk stop-loss (naming collision between the two modules).
                last_stop_price = round(entry_price, 6)
                last_stop_type = "BUY_STOP" if direction == config.DIRECTION_BULLISH else "SELL_STOP"
                last_risk_stop_price = round(stop_loss_price, 6)
                last_target_price = round(target_price, 6)
                last_trigger_time = df_ltf.index[i]
                last_filled_time = None

        state = new_state

    last_row = df_ltf.iloc[-1]
    current_state = {
        "state": state,
        "price": round(float(last_row["close"]), 6),
        "lsma": round(float(last_row["lsma"]), 6) if not pd.isna(last_row["lsma"]) else None,
        "macd_gap_pct": round(float(last_macd_gap_pct), 4) if last_macd_gap_pct not in (None, float("inf")) else None,
    }
    if state == config.STATE_TRIGGERED and last_stop_price is not None:
        current_state["stop_price"] = last_stop_price
        current_state["stop_type"] = last_stop_type
        current_state["risk_stop_price"] = last_risk_stop_price
        current_state["target_price"] = last_target_price
        current_state["filled"] = filled
        current_state["trigger_time"] = to_utc_iso(last_trigger_time) if last_trigger_time is not None else None
        if filled and last_filled_time is not None:
            current_state["filled_time"] = to_utc_iso(last_filled_time)

    return trades, current_state


def simulate_outcome(df: pd.DataFrame, fill_idx: int, setup: Setup) -> str:
    n = len(df)
    end = min(n, fill_idx + 1 + MAX_HOLD_BARS)
    for j in range(fill_idx + 1, end):
        high, low = df["high"].iloc[j], df["low"].iloc[j]
        if setup.direction == config.DIRECTION_BULLISH:
            hit_target = high >= setup.target_price
            hit_stop = low <= setup.stop_loss_price
        else:
            hit_target = low <= setup.target_price
            hit_stop = high >= setup.stop_loss_price
        if hit_stop and hit_target:
            return "LOSS"  # ambiguous same-bar overlap — conservative
        if hit_stop:
            return "LOSS"
        if hit_target:
            return "WIN"
    return "UNRESOLVED"


def backtest_asset_timeframe(asset: dict, higher_tf: str) -> list:
    df_htf = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], higher_tf)
    if df_htf is None or len(df_htf) < config.MIN_WARMUP_BARS:
        return []
    df_htf = indicators.compute_all(df_htf)
    bias_df = htf_bias_series(df_htf)
    # anchor = the actual timestamp of each bar's current trigger bar, so a
    # same-direction re-anchor (a fresher extreme superseding an older one,
    # per htf_bias_series) is still detectable after merge_asof onto the
    # lower tf — trigger_idx alone wouldn't survive the join meaningfully.
    htf_anchor_time = [
        df_htf.index[int(t)] if t is not None else None
        for t in bias_df["trigger_idx"]
    ]

    all_trades = []
    for entry_tf in config.LOWER_TF_MAP[higher_tf]:
        df_ltf = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], entry_tf)
        if df_ltf is None or len(df_ltf) < config.MIN_WARMUP_BARS:
            continue
        df_ltf = indicators.compute_all(df_ltf)

        idx_htf = bias_df.index.tz_localize(None) if bias_df.index.tz is not None else bias_df.index
        idx_ltf = df_ltf.index.tz_localize(None) if df_ltf.index.tz is not None else df_ltf.index
        merged = pd.merge_asof(
            pd.DataFrame({"t": idx_ltf}).sort_values("t"),
            pd.DataFrame({"t": idx_htf, "bias": bias_df["bias"].values, "anchor": htf_anchor_time}).sort_values("t"),
            on="t", direction="backward",
        )
        bias_aligned = pd.DataFrame({"bias": merged["bias"].values, "anchor": merged["anchor"].values})
        bias_aligned.index = df_ltf.index

        trades, _ = replay_lower_tf(df_ltf, bias_aligned, asset, higher_tf, entry_tf)
        all_trades.extend(trades)
    return all_trades


def summarize(trades: list) -> dict:
    wins = sum(1 for t in trades if t.outcome == "WIN")
    losses = sum(1 for t in trades if t.outcome == "LOSS")
    unresolved = sum(1 for t in trades if t.outcome == "UNRESOLVED")
    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved else None
    expectancy_r = ((wins * 1) + (losses * -1)) / resolved if resolved else None
    return {
        "total_trades": len(trades), "wins": wins, "losses": losses, "unresolved": unresolved,
        "resolved": resolved, "win_rate_pct": win_rate, "expectancy_r": expectancy_r,
    }


def run(universe: list) -> None:
    all_trades = []
    for asset in universe:
        for htf in config.HIGHER_TIMEFRAMES:
            trades = backtest_asset_timeframe(asset, htf)
            if trades:
                logger.info("%s %s: %d trades", asset["display_name"], htf, len(trades))
            all_trades.extend(trades)

    summary = summarize(all_trades)
    logger.info("=== OVERALL ===")
    logger.info(
        "%d trades | %d wins / %d losses (win rate %.1f%%) | %d unresolved | expectancy %.2fR",
        summary["total_trades"], summary["wins"], summary["losses"],
        summary["win_rate_pct"] or 0.0, summary["unresolved"], summary["expectancy_r"] or 0.0,
    )
    return all_trades, summary


if __name__ == "__main__":
    import scan
    run(scan.build_universe())
