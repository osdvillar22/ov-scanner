"""
config.py — single source of truth for the scanner.

Everything that's a "tunable knob" lives here so scan.py / fetch.py /
indicators.py never hardcode a threshold or a ticker list inline.
"""

# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------

# The timeframes we scan the FULL universe on, looking for an RSI extreme.
HIGHER_TIMEFRAMES = ["1H", "4H", "1D", "1W"]

# Purely for the dashboard's two extra reference charts per watchlisted
# asset — these are NOT scanned or tracked for any kind of setup/state.
LOWER_TF_MAP = {
    "1H": ["15m", "5m"],
    "4H": ["1H", "15m"],
    "1D": ["4H", "1H"],
    "1W": ["1D", "4H"],
}

# yfinance's native intraday intervals. Anything not in this dict (4H) has to
# be resampled from 1H bars ourselves — see fetch.resample_to_4h().
YFINANCE_NATIVE_INTERVAL = {
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1H": "60m",
    "1D": "1d",
    "1W": "1wk",
}

# Kraken's OHLC endpoint takes interval-in-minutes and supports all of these
# natively — no resampling needed for crypto.
KRAKEN_NATIVE_INTERVAL = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
    "1W": 10080,
}

# yfinance's unofficial intraday endpoint only keeps ~60 days of history for
# sub-daily intervals. We ask for the max allowed per interval; anything
# beyond this is simply unavailable for free.
YFINANCE_MAX_INTRADAY_PERIOD = {
    "5m": "60d",
    "15m": "60d",
    "30m": "60d",
    "60m": "730d",  # Yahoo allows longer history for 60m than for 15m/30m
}

# ---------------------------------------------------------------------------
# Indicator parameters
# ---------------------------------------------------------------------------

RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

EMA_PERIODS = [10, 20]

LSMA_LENGTH = 50
LSMA_OFFSET = 3

# How close MACD and Signal need to be (as a fraction of |MACD|) to count as
# "converging" ahead of an actual cross. 2% was chosen over 5% deliberately —
# see learnings.md: 5% flagged too early and cluttered the feed.
MACD_CLOSENESS_PCT = 0.02

# Minimum bars of history to fetch so every indicator is fully warmed up
# before we trust its value. LSMA(50) needs 50+, MACD needs ~35 for the
# signal line to stabilize — 250 gives generous headroom for both plus
# some cushion for missing/holiday bars.
MIN_WARMUP_BARS = 250

# ---------------------------------------------------------------------------
# State machine — used only by backtest.py's standalone pullback-entry
# research tool now. The live scanner (scan.py) is Phase A only: it just
# watchlists RSI extremes, it no longer tracks a WATCHING -> PULLBACK ->
# CONVERGING -> TRIGGERED entry setup.
# ---------------------------------------------------------------------------

STATE_WATCHING = "WATCHING"
STATE_PULLBACK = "PULLBACK"
STATE_CONVERGING = "CONVERGING"
STATE_TRIGGERED = "TRIGGERED"

DIRECTION_BULLISH = "BULLISH"  # RSI > 70 -> bullish continuation bias
DIRECTION_BEARISH = "BEARISH"  # RSI < 30 -> bearish continuation bias

# How many candles of the *triggering* (higher) timeframe an asset stays
# visible for once it enters WATCHING — even if RSI reverts back inside
# 30/70 partway through. A fresh RSI extreme within this window always
# re-anchors the count and the trigger candle to itself (see
# backtest.htf_trigger_origin) instead of staying pinned to the original one.
VISIBILITY_WINDOW_CANDLES = 20

# How many trailing candles of OHLC + indicator history to persist per
# (asset, timeframe) into state.json/data.json for dashboard.html's charts.
DASHBOARD_CANDLE_WINDOW = 120

# ---------------------------------------------------------------------------
# Asset universe
# ---------------------------------------------------------------------------
#
# PSE (Philippine Stock Exchange) is intentionally excluded for now: yfinance
# has no working ".PS"-suffix mapping for PSE tickers (confirmed via
# yfinance.Search — PH names only resolve to unrelated US OTC ADRs like
# SVTMF), and the free community alternatives are dead (pselookup.vrymel.com
# no longer resolves) or static one-off dumps (kiosklabs' CSV export, not
# live). The one live/current option found (EODHD) gates non-US exchanges
# behind a paid plan, and none of the free options offer intraday data
# regardless — only EOD — so even a fix would only cover the 1D/1W scan, not
# the 1H/4H lower-timeframe pullback logic. Revisit if a paid data feed or
# broker API becomes available.

FOREX_PAIRS = {
    # display_name: yfinance_ticker
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    # Non-standard construction, per learnings.md: yfinance quotes this as
    # PHP=X (i.e. "USD/PHP" without the leading USD in the symbol).
    "USDPHP": "PHP=X",
}

METALS = {
    # display_name: yfinance_ticker (using futures symbols — more reliable
    # on yfinance's free endpoint than the XAU/XAG spot-proxy tickers)
    "GOLD": "GC=F",
    "SILVER": "SI=F",
    "PLATINUM": "PL=F",
    "PALLADIUM": "PA=F",
}

INDICES = {
    # display_name: yfinance index ticker (Yahoo's "^"-prefixed symbols)
    "US30": "^DJI",
    "US500": "^GSPC",
    "USNAS100": "^IXIC",
    "UK100": "^FTSE",
    "GER40": "^GDAXI",
    "JPN225": "^N225",
    "HK50": "^HSI",
    "FRA40": "^FCHI",
    "AUS200": "^AXJO",
    "INDIA_SENSEX": "^BSESN",
}

ENERGY = {
    "OIL_WTI": "CL=F",
    "OIL_BRENT": "BZ=F",
}

# Kraken, not Binance: Binance.com geo-blocks GitHub Actions' US-based
# runner IPs with HTTP 451 (confirmed in production — see git history).
# Kraken is US-licensed and has no such block. Its dollar-quoted pairs use
# the asset code "ZUSD", not "USD" — that's Kraken's own convention.
KRAKEN_BASE_URL = "https://api.kraken.com"
KRAKEN_QUOTE_ASSET = "ZUSD"  # crypto universe = all Kraken USD pairs

# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

STATE_FILE = "state.json"    # persisted between runs — NOT committed to git (see
                              # .gitignore); with candle history attached it's 8MB+
                              # and churns every run, so CI needs to persist it via
                              # a runner cache/artifact, not a git commit.
OUTPUT_FILE = "data.json"    # what dashboard.html reads — same story, not committed

DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"  # set as a GitHub Actions secret
