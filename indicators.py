"""
indicators.py — pure functions over a pandas close-price Series.

Every function returns a pandas Series aligned to the input index, so you
can always do `df['rsi'] = compute_rsi(df['close'])` and stay in one frame.
No fetching, no state — just math, so it's easy to unit-test in isolation.
"""

import numpy as np
import pandas as pd


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder's RSI (the standard TradingView/broker-terminal formula).

    Uses Wilder's smoothing (alpha = 1/period), NOT a plain SMA of gains/
    losses — that's the detail that most naive RSI implementations get
    wrong and end up off from what your charting platform shows.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    # Where avg_loss is exactly 0 (straight-up move), RSI is 100 by definition.
    rsi = rsi.where(avg_loss != 0, 100)
    return rsi


def compute_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Returns (macd_line, signal_line, histogram)."""
    ema_fast = compute_ema(series, fast)
    ema_slow = compute_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_lsma(series: pd.Series, length: int = 50, offset: int = 3) -> pd.Series:
    """
    Rolling least-squares moving average, matching Pine Script's
    ta.linreg(src, length, offset) convention:

    For each window of `length` bars ending at the current bar, fit a
    straight line (degree-1 polyfit) through it, then evaluate that line
    at position (length - 1 - offset) bars from the start of the window.
    offset=0 evaluates at the most recent bar (a pure linear-regression
    moving average); a positive offset looks slightly further back.

    NaN for the first (length - 1) bars, same as any rolling indicator.
    """
    values = series.to_numpy(dtype=float)
    n = len(values)
    out = np.full(n, np.nan)
    x = np.arange(length, dtype=float)
    eval_x = length - 1 - offset

    for i in range(length - 1, n):
        window = values[i - length + 1 : i + 1]
        if np.isnan(window).any():
            continue
        slope, intercept = np.polyfit(x, window, 1)
        out[i] = slope * eval_x + intercept

    return pd.Series(out, index=series.index, name=f"lsma_{length}_{offset}")


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR (TradingView's default ATR)."""
    prev_close = df["close"].shift()
    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def compute_all(df: pd.DataFrame, close_col: str = "close") -> pd.DataFrame:
    """
    Convenience wrapper: takes an OHLC dataframe, returns it with every
    indicator column from config attached. Import config lazily to avoid
    a circular import if indicators.py is ever used standalone.
    """
    import config

    out = df.copy()
    close = out[close_col]

    out["rsi"] = compute_rsi(close, config.RSI_PERIOD)

    macd_line, signal_line, hist = compute_macd(
        close, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL
    )
    out["macd"] = macd_line
    out["macd_signal"] = signal_line
    out["macd_hist"] = hist

    for period in config.EMA_PERIODS:
        out[f"ema{period}"] = compute_ema(close, period)

    out["lsma"] = compute_lsma(close, config.LSMA_LENGTH, config.LSMA_OFFSET)

    # Plain simple moving average — a chart line and the SMA 50 watch level.
    out[f"sma{config.SMA_PERIOD}"] = close.rolling(config.SMA_PERIOD).mean()
    out["atr"] = compute_atr(out, config.ATR_PERIOD)

    return out
