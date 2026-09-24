"""
config.py — single source of truth for the scanner.

Everything that's a "tunable knob" lives here so scan.py / fetch.py /
indicators.py never hardcode a threshold or a ticker list inline.
"""

# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------

# The timeframes we scan the FULL universe on, looking for a watch trigger.
# 1W is deliberately excluded: its own AUTO_HIGHER_TF step would be "1M",
# and Kraken's OHLC endpoint can't supply enough monthly history for
# crypto to ever satisfy that condition (~24 bars from its ~720-day cap,
# short of LSMA(50)'s minimum) — rather than have 1W work for some asset
# classes and never for others, it's dropped entirely.
HIGHER_TIMEFRAMES = ["1H", "4H", "1D"]

# Purely for the dashboard's two extra reference charts per watchlisted
# asset — these are NOT scanned or tracked for any kind of setup/state.
LOWER_TF_MAP = {
    "1H": ["15m", "5m"],
    "4H": ["1H", "15m"],
    "1D": ["4H", "1H"],
}

# The "one step up" timeframe used only for the watch-trigger's cross-
# timeframe LSMA confirmation (see scan.find_phase_a_origin) — never
# scanned or watchlisted on its own, and never shown on the dashboard.
# "1W" is still fetched here (1D's own auto-tf), just no longer a key —
# 1W itself isn't scanned, see the HIGHER_TIMEFRAMES note above.
AUTO_HIGHER_TF = {
    "1H": "4H",
    "4H": "1D",
    "1D": "1W",
}

# Bar length in minutes for every timeframe we ever fetch — used to convert
# "N candles on the higher timeframe" into an equivalent candle count on a
# lower timeframe (see scan.lower_tf_candle_count).
TIMEFRAME_MINUTES = {
    "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240, "1D": 1440, "1W": 10080,
}

# The watch-trigger window: an asset is watchlisted if the entry condition
# fired on ANY of the trailing WATCH_WINDOW_CANDLES candles on that higher
# timeframe, and comes off the watchlist the moment none of them still do,
# or once a finished candle after the most recent trigger closes on the
# wrong side of EMA20 (see scan.find_phase_a_watch). Also
# doubles as the trigger-timeframe chart's visible candle count, so the
# dashboard always shows exactly the window the logic is evaluating.
WATCH_WINDOW_CANDLES = 25

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

# The AUTO_HIGHER_TF lookup only ever needs that timeframe's LSMA — not
# RSI/MACD — so it doesn't need MIN_WARMUP_BARS' full headroom.
LSMA_WARMUP_BARS = LSMA_LENGTH + 10

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

# Used only by backtest.py's standalone RSI-only research tool now. The
# live scanner's Phase A watchlist (scan.find_phase_a_origin) has no fixed
# candle cap — an asset stays watchlisted until the trend-break invalidation
# condition fires, however long that takes.
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

# The user's hand-picked basket (see basket.py). Committed to git — the
# dashboard writes it via the GitHub API, the workflows read it from the
# checkout.
BASKET_FILE = "basket.json"
# Which lower-tf candles already fired an entry alert, one file per workflow
# (persisted via the Actions cache, like state.json).
BASKET_ALERTS_CRYPTO_FILE = "basket_alerts_crypto.json"
BASKET_ALERTS_HOURLY_FILE = "basket_alerts_hourly.json"

# Discord webhook for basket entry alerts, set as a GitHub Actions secret.
# (The old new-watch alerts on DISCORD_WEBHOOK_URL were retired.)
DISCORD_ENTRY_WEBHOOK_ENV = "DISCORD_ENTRY_WEBHOOK_URL"
