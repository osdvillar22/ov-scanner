"""
make_crypto_categories.py — (re)build crypto_categories.json: one main
category per crypto the scanner trades on Kraken, from CoinGecko's
category lists. Run by hand now and then (new listings land in "Other"
until the next run):

    python make_crypto_categories.py

Each Kraken ticker is matched to the biggest CoinGecko coin with that
symbol (so a copycat "XRP" meme token can't claim XRP), from the top
ID_PAGES x 250 by market cap. A coin in several CoinGecko categories gets
the first one in PRIORITY (narrow narratives before broad ones, e.g. a
meme coin on its own chain is Meme, not L1); each display category takes
several CoinGecko ones, so a coin missing from the main list (DeFi) can
still match a narrower one (lending, perpetuals).

CoinGecko's market-cap ranking leaves out staked / wrapped tokens and
stablecoins, so for those categories a coin is also matched by symbol
inside the category's own list. Stablecoins are kept off the board (they
sit at 0% and only drag the medians to 0). Only coins missing from the
ranking are matched that way.
Hand fixes go in the file's "overrides" — kept on rebuild.
"""

import json
import logging
import time
from pathlib import Path

import requests

import fetch

OUT = Path("crypto_categories.json")
API = "https://api.coingecko.com/api/v3/coins/markets"

# Display name -> CoinGecko category ids, in priority order.
PRIORITY = [
    ("Stablecoins", ["stablecoins"]),
    # Staked / wrapped copies of SOL, ETH, BTC: they move with those, not DeFi.
    ("Staked/Wrapped", ["liquid-staking-tokens", "liquid-restaking-tokens", "tokenized-btc", "wrapped-tokens"]),
    ("Meme", ["meme-token"]),
    ("AI", ["artificial-intelligence"]),
    ("Gaming", ["gaming", "play-to-earn", "metaverse"]),
    ("NFT", ["non-fungible-tokens-nft", "nft-marketplace"]),
    ("Privacy", ["privacy-coins"]),
    ("Exchange", ["exchange-based-tokens", "centralized-exchange-token-cex"]),
    ("L2", ["layer-2"]),
    ("Oracles/Infra", ["oracle", "depin", "storage", "interoperability", "identity", "name-service", "wallets"]),
    ("L1", ["layer-1", "smart-contract-platform"]),
    ("RWA", ["real-world-assets-rwa"]),
    ("DeFi", ["decentralized-finance-defi", "decentralized-exchange", "lending-borrowing",
              "decentralized-derivatives", "decentralized-perpetuals", "yield-farming"]),
    ("Payments", ["payment-solutions"]),
]
# Matched by symbol inside the category list too (see above).
SYMBOL_MATCH = {"Stablecoins", "Staked/Wrapped"}
# Not shown on the crypto board.
HIDDEN = {"Stablecoins"}
# Hand fixes: big chains CoinGecko also files under AI; Kraken symbols
# that differ from CoinGecko's (NANO is XNO there, METH is Mantle's
# staked ETH, LUNA is Terra's original chain).
DEFAULT_OVERRIDES = {"NEAR": "L1", "ICP": "L1", "DOT": "L1", "TON": "L1",
                     "NANO": "Payments", "METH": "Staked/Wrapped", "LUNA": "L1"}
OTHER = "Other"
MAX_PAGES = 16
ID_PAGES = 20


def _page(params: dict) -> list:
    for _ in range(5):
        resp = requests.get(API, params={"vs_currency": "usd", "per_page": 250, **params}, timeout=30)
        if resp.status_code == 429:
            time.sleep(30)
            continue
        resp.raise_for_status()
        time.sleep(3)
        return resp.json()
    raise RuntimeError("CoinGecko kept rate-limiting")


def coin_ids(wanted: set) -> dict:
    """Symbol -> CoinGecko id of the biggest coin with that symbol, from
    the top ID_PAGES x 250 by market cap."""
    ids = {}
    for page in range(1, ID_PAGES + 1):
        rows = _page({"page": page})
        for r in rows:
            ids.setdefault(r["symbol"].upper(), r["id"])
        if len(rows) < 250 or wanted <= set(ids):
            break
    return {s: i for s, i in ids.items() if s in wanted}


def members(category_id: str, wanted_ids: set, wanted_syms: set = frozenset()) -> tuple:
    """CoinGecko ids from `wanted_ids` listed in a category, and the
    symbols from `wanted_syms` found there (biggest coin first)."""
    found, syms = set(), {}
    for page in range(1, MAX_PAGES + 1):
        rows = _page({"category": category_id, "page": page})
        found |= {r["id"] for r in rows} & wanted_ids
        for r in rows:
            if r["symbol"].upper() in wanted_syms:
                syms.setdefault(r["symbol"].upper(), r["id"])
        if len(rows) < 250 or (wanted_ids <= found and wanted_syms <= set(syms)):
            break
    return found, syms


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    coins = {p["display_name"].split("/")[0] for p in fetch.get_kraken_usd_pairs() if not p.get("tag")}
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    overrides = {**DEFAULT_OVERRIDES, **old.get("overrides", {})}

    ids = coin_ids(coins)
    sym_of = {i: s for s, i in ids.items()}
    logging.info("%d of %d coins found on CoinGecko", len(ids), len(coins))
    cat_of = {}
    for name, cids in PRIORITY:
        for cid in cids:
            # Only coins missing from the ranking: a listed coin (BTC) must
            # not match its wrapped copy's symbol.
            by_sym = {s for s in coins if s not in cat_of and s not in ids} if name in SYMBOL_MATCH else set()
            got, syms = members(cid, {i for s, i in ids.items() if s not in cat_of}, by_sym)
            for i in got:
                cat_of.setdefault(sym_of[i], name)
            for s in syms:
                cat_of.setdefault(s, name)
            logging.info("%-14s %-28s +%d (+%d by symbol)", name, cid, len(got), len(syms))
    for sym in coins:
        cat_of.setdefault(sym, OTHER)
    cat_of.update({k: v for k, v in overrides.items() if k in coins})

    OUT.write_text(json.dumps({
        "categories": [n for n, _ in PRIORITY if n not in HIDDEN] + [OTHER],
        "hidden": sorted(HIDDEN),
        "overrides": overrides,
        "coins": dict(sorted(cat_of.items())),
    }, indent=1) + "\n")
    counts = {}
    for c in cat_of.values():
        counts[c] = counts.get(c, 0) + 1
    logging.info("%d coins: %s", len(cat_of), counts)


if __name__ == "__main__":
    main()
