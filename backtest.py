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


def htf_bias_series(df_htf: pd.DataFrame) -> pd.Series:
    """Bar-by-bar active direction bias, replaying scan.py's WATCHING window."""
    rsi = df_htf["rsi"]
    bias = pd.Series(index=df_htf.index, dtype=object)
    active_direction = None
    candles_since_trigger = 0
    for i in range(len(df_htf)):
        r = rsi.iloc[i]
        if pd.isna(r):
            bias.iloc[i] = None
            continue
        is_ob = r > config.RSI_OVERBOUGHT
        is_os = r < config.RSI_OVERSOLD
        if active_direction is None:
            if is_ob:
                active_direction = config.DIRECTION_BULLISH
                candles_since_trigger = 0
            elif is_os:
                active_direction = config.DIRECTION_BEARISH
                candles_since_trigger = 0
        else:
            candles_since_trigger += 1
            if candles_since_trigger > config.VISIBILITY_WINDOW_CANDLES:
                active_direction = None
                candles_since_trigger = 0
                if is_ob:
                    active_direction = config.DIRECTION_BULLISH
                elif is_os:
                    active_direction = config.DIRECTION_BEARISH
        bias.iloc[i] = active_direction
    return bias


def replay_lower_tf(df_ltf: pd.DataFrame, bias: pd.Series, asset: dict, higher_tf: str, entry_tf: str) -> list:
    """Walk the lower-tf bars in order, replaying PULLBACK/CONVERGING/TRIGGERED,
    and simulate fill + win/loss/unresolved for every setup it produces."""
    trades: list[Trade] = []
    state = config.STATE_WATCHING
    direction = None
    pullback_low = None
    pullback_high = None
    pending_setup: Setup | None = None
    fill_wait = 0

    n = len(df_ltf)
    for i in range(n):
        row = df_ltf.iloc[i]
        bar_bias = bias.iloc[i]
        close, high, low = row["close"], row["high"], row["low"]
        lsma, macd, macd_signal, macd_hist = row["lsma"], row["macd"], row["macd_signal"], row["macd_hist"]

        # Handle a pending (TRIGGERED but not yet filled) setup first.
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
            elif fill_wait > FILL_TIMEOUT_BARS:
                pending_setup = None

        if bar_bias is None or pd.isna(lsma) or pd.isna(macd):
            state = config.STATE_WATCHING
            direction = None
            continue

        if bar_bias != direction:
            # Bias changed (new trigger, flipped, or expired) — reset the lower-tf machine.
            direction = bar_bias
            state = config.STATE_WATCHING
            pullback_low = pullback_high = None

        if direction == config.DIRECTION_BULLISH:
            on_wrong_side = close < lsma
            macd_confirmed = macd_hist > 0
        else:
            on_wrong_side = close > lsma
            macd_confirmed = macd_hist < 0

        macd_gap_pct = abs(macd - macd_signal) / abs(macd) if macd != 0 else float("inf")
        macd_close = macd_gap_pct <= config.MACD_CLOSENESS_PCT

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

        state = new_state

    return trades


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
    bias_htf = htf_bias_series(df_htf)
    bias_df = pd.DataFrame({"bias": bias_htf})

    all_trades = []
    for entry_tf in config.LOWER_TF_MAP[higher_tf]:
        df_ltf = fetch.fetch_ohlc(asset["asset_class"], asset["ticker"], entry_tf)
        if df_ltf is None or len(df_ltf) < config.MIN_WARMUP_BARS:
            continue
        df_ltf = indicators.compute_all(df_ltf)

        idx_htf = bias_df.index.tz_localize(None) if bias_df.index.tz is not None else bias_df.index
        idx_ltf = df_ltf.index.tz_localize(None) if df_ltf.index.tz is not None else df_ltf.index
        bias_aligned = pd.merge_asof(
            pd.DataFrame({"t": idx_ltf}).sort_values("t"),
            pd.DataFrame({"t": idx_htf, "bias": bias_df["bias"].values}).sort_values("t"),
            on="t", direction="backward",
        )["bias"]
        bias_aligned.index = df_ltf.index

        trades = replay_lower_tf(df_ltf, bias_aligned, asset, higher_tf, entry_tf)
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
