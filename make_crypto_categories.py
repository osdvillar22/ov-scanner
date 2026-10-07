"""
make_crypto_categories.py — (re)build crypto_categories.json: one main
category per crypto the scanner trades on Kraken, from CoinGecko's
category lists. Run by hand now and then (new listings land in "Other"
until the next run):

    python make_crypto_categories.py

Each Kraken ticker is matched to the biggest CoinGecko coin with that
symbol (so a copycat "XRP" meme token can't claim XRP). A coin in several
CoinGecko categories gets the first one in PRIORITY (narrow narratives
before broad ones, e.g. a meme coin on its own chain is Meme, not L1).
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
    ("Meme", ["meme-token"]),
    ("AI", ["artificial-intelligence"]),
    ("Gaming", ["gaming"]),
    ("Privacy", ["privacy-coins"]),
    ("Exchange", ["exchange-based-tokens"]),
    ("L2", ["layer-2"]),
    ("Oracles/Infra", ["oracle", "depin"]),
    ("L1", ["layer-1", "smart-contract-platform"]),
    ("RWA", ["real-world-assets-rwa"]),
    ("DeFi", ["decentralized-finance-defi"]),
    ("Payments", ["payment-solutions"]),
]
# Starting hand fixes (big chains CoinGecko also files under AI).
DEFAULT_OVERRIDES = {"NEAR": "L1", "ICP": "L1", "DOT": "L1"}
OTHER = "Other"
MAX_PAGES = 8


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
    the top 2000 by market cap."""
    ids = {}
    for page in range(1, MAX_PAGES + 1):
        rows = _page({"page": page})
        for r in rows:
            ids.setdefault(r["symbol"].upper(), r["id"])
        if len(rows) < 250 or wanted <= set(ids):
            break
    return {s: i for s, i in ids.items() if s in wanted}


def members(category_id: str, wanted_ids: set) -> set:
    """CoinGecko ids from `wanted_ids` listed in a category."""
    found = set()
    for page in range(1, MAX_PAGES + 1):
        rows = _page({"category": category_id, "page": page})
        found |= {r["id"] for r in rows} & wanted_ids
        if len(rows) < 250 or wanted_ids <= found:
            break
    return found


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    coins = {p["display_name"].split("/")[0] for p in fetch.get_kraken_usd_pairs() if not p.get("tag")}
    old = json.loads(OUT.read_text()) if OUT.exists() else {}
    overrides = old.get("overrides", DEFAULT_OVERRIDES)

    ids = coin_ids(coins)
    sym_of = {i: s for s, i in ids.items()}
    logging.info("%d of %d coins found on CoinGecko", len(ids), len(coins))
    cat_of = {}
    for name, cids in PRIORITY:
        for cid in cids:
            got = members(cid, {i for s, i in ids.items() if s not in cat_of})
            for i in got:
                cat_of.setdefault(sym_of[i], name)
            logging.info("%-14s %-28s +%d", name, cid, len(got))
    for sym in coins:
        cat_of.setdefault(sym, OTHER)
    cat_of.update({k: v for k, v in overrides.items() if k in coins})

    OUT.write_text(json.dumps({
        "categories": [n for n, _ in PRIORITY] + [OTHER],
        "overrides": overrides,
        "coins": dict(sorted(cat_of.items())),
    }, indent=1) + "\n")
    counts = {}
    for c in cat_of.values():
        counts[c] = counts.get(c, 0) + 1
    logging.info("%d coins: %s", len(cat_of), counts)


if __name__ == "__main__":
    main()
