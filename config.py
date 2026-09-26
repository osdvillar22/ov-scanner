"""
config.py — single source of truth for the scanner.

Everything that's a "tunable knob" lives here so scan.py / fetch.py /
indicators.py never hardcode a threshold or a ticker list inline.
"""

# ---------------------------------------------------------------------------
# Timeframes
# ---------------------------------------------------------------------------

# The timeframes we scan the FULL universe on, looking for a watch trigger.
# 1W is added per asset class below (EXTRA_HIGHER_TIMEFRAMES).
HIGHER_TIMEFRAMES = ["1H", "4H", "1D"]

# Purely for the dashboard's two extra reference charts per watchlisted
# asset — these are NOT scanned or tracked for any kind of setup/state.
LOWER_TF_MAP = {
    "1H": ["15m", "5m"],
    "4H": ["1H", "15m"],
    "1D": ["4H", "1H"],
    "1W": ["1D", "4H"],  # PSE and crypto, see EXTRA_HIGHER_TIMEFRAMES
}

# PSE and crypto also get a weekly watch. It has no AUTO_HIGHER_TF — its
# trigger is the other three conditions without the LSMA-vs-next-timeframe
# check (the user's choice: a monthly LSMA(50) needs 50+ months, far more
# than either source gives). Both build 1W from daily candles, weeks
# starting Monday like TradingView, so it costs no extra request.
EXTRA_HIGHER_TIMEFRAMES = {"pse": ["1W"], "crypto": ["1W"]}
# Warm-up floor per timeframe where MIN_WARMUP_BARS (250) can't be met —
# 800 daily bars make ~160 weekly ones, plenty for EMA20/RSI14.
MIN_BARS_BY_TF = {"1W": 60}

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

# The SMA 50 pullback watch (see scan.find_sma50_watch). After a trend watch
# is removed (close past EMA20), a candle from that removal candle through
# the next SMA50_SEARCH_CANDLES that reaches SMA 50 — its low within
# SMA50_TOUCH_ATR x ATR14 above it (bullish; mirrored for bearish), or
# through it — starts an SMA 50 watch lasting SMA50_WATCH_CANDLES candles
# counted from that touch candle. Its basket entries fire once per line.
SMA50_SEARCH_CANDLES = 25
SMA50_WATCH_CANDLES = 10
SMA50_TOUCH_ATR = 0.25
ATR_PERIOD = 14

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

# Kraken's OHLC endpoint takes interval-in-minutes. 1W is NOT fetched
# natively — Kraken's weeks start on Thursday; fetch.py builds Monday weeks
# from the 1D candles instead (~720 days -> ~100 weeks).
KRAKEN_NATIVE_INTERVAL = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1H": 60,
    "4H": 240,
    "1D": 1440,
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

# SMA 50: a chart line, and the level of the SMA 50 pullback watch.
SMA_PERIOD = 50

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
# PSE (Philippine Stock Exchange): yfinance has no working PSE mapping, and
# every free official/community source is end-of-day only (PSE EDGE's chart
# data also lags a trading day). Intraday comes from TradingView instead —
# free, ~15 min delayed, all timeframes — via the unofficial tvdatafeed
# library (see fetch.fetch_tradingview_ohlc). It's not a sanctioned API and
# can break or be blocked without notice. The stock list itself comes from
# PSE EDGE's public company directory (~283 listings incl. ETFs).
PSE_TV_EXCHANGE = "PSE"
# The user's list of PSE codes to leave out of the scan (see the file).
PSE_EXCLUDED_FILE = "pse_excluded.txt"
PSE_EDGE_DIRECTORY_URL = "https://edge.pse.com.ph/companyDirectory/search.ax"
PSE_EDGE_RATE_LIMIT_SECONDS = 0.6  # be polite to a small exchange's site

# TradingView interval per timeframe (tvdatafeed Interval member names).
TV_INTERVAL = {
    "5m": "in_5_minute", "15m": "in_15_minute", "30m": "in_30_minute",
    "1H": "in_1_hour", "4H": "in_4_hour", "1D": "in_daily", "1W": "in_weekly",
}
# Bars per TradingView request — enough warm-up plus the longest lower-tf
# chart window (1D watch -> 600 x 1H), well under TradingView's 5000 cap.
# 1D/1W need far fewer, and big daily requests were the ones dropping.
TV_BARS = 1000
TV_BARS_LONG_TF = {"1D": 800, "1W": 300}  # 1D: ~160 weeks for the PSE 1W watch

# PSE trading session (Asia/Manila), for the 5-minute basket check — outside
# it there are no new candles, so PSE picks are skipped. Ends a little after
# the 15:00 close so the last (delayed) candles still get checked. Doesn't
# know PSE holidays; a check on one is just a harmless no-op.
PSE_TIMEZONE = "Asia/Manila"
PSE_SESSION = ("09:30", "15:30")

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

# Display names come from Kraken's "wsname" (e.g. "XBT/USD") — its pair keys
# like "XXBTZUSD" are unreadable. These are Kraken's own non-standard codes.
KRAKEN_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}

# Stablecoins are left out of the scan entirely — pegged, so they barely
# move and only add noise. Keyed by wsname base.
KRAKEN_EXCLUDED_BASES = {
    "USDT", "USDC", "DAI", "PYUSD", "RLUSD", "USDE", "USDG", "USDD", "USD1", "USDS",
    "USDQ", "USDGO", "USDPT", "USDSM", "USDUC", "AUSD", "EURC", "EURQ", "EUROP",
    "TGBP", "QCAD", "AUDX", "BRL1", "MXNB",
}

# "Large caps" for PSE = the 30 PSEi constituents, effective 2026-08-03
# (MYNLD in for CNVRG). Always scanned, even if listed in pse_excluded.txt.
# PSE reviews the index each January and July — update this list then.
PSE_LARGE_CAPS = {
    "AC", "ACEN", "AEV", "ALI", "AREIT", "BDO", "BPI", "CBC", "CNPF", "DMC",
    "EMI", "GLO", "GTCAP", "ICT", "JFC", "JGS", "LTG", "MBT", "MER", "MONDE",
    "MYNLD", "PGOLD", "PLUS", "RCR", "SCC", "SM", "SMC", "SMPH", "TEL", "URC",
}

# "Large caps" for the dashboard's crypto filter — a fixed list, by
# wsname base (after KRAKEN_BASE_ALIASES). From CoinGecko's market-cap
# ranking on 2026-09-25: the top 20 non-stablecoins with a Kraken USD pair,
# with WBT and CC swapped for SUI and HBAR at the user's request.
CRYPTO_LARGE_CAPS = {
    "BTC", "ETH", "BNB", "XRP", "SOL", "TRX", "ZEC", "HYPE", "DOGE", "XMR",
    "LINK", "ADA", "XLM", "BCH", "NEAR", "UNI", "LTC", "AVAX", "SUI", "HBAR",
}

# The rest of Kraken's USD list that isn't crypto stays in the scan (its
# data is near-real-time), just labelled on the dashboard so it's not
# mistaken for a coin. Keyed by wsname base.
KRAKEN_ASSET_TAGS = {
    **{c: "currency" for c in ("AUD", "EUR", "GBP")},
    "PAXG": "gold", "XAUT": "gold",
    "XU3O8": "uranium",
}

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
# "Live" = the 5-minute workflow's picks: crypto, and PSE during its session.
# (Filename kept from when it was crypto-only, so its Actions cache carries over.)
BASKET_ALERTS_LIVE_FILE = "basket_alerts_crypto.json"
BASKET_ALERTS_HOURLY_FILE = "basket_alerts_hourly.json"

# Discord webhook for basket entry alerts, set as a GitHub Actions secret.
# (The old new-watch alerts on DISCORD_WEBHOOK_URL were retired.)
DISCORD_ENTRY_WEBHOOK_ENV = "DISCORD_ENTRY_WEBHOOK_URL"
