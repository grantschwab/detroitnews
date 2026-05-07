"""
Pulls Michigan election markets from Kalshi's public trade API (no auth required).
Outputs a timestamped CSV with one row per race, candidates in wide format.

Run: python kalshi_mi.py
Output: data/kalshi_snapshot_YYYYMMDD_HHMMSS.csv

Series tickers are hardcoded — Kalshi has no keyword/tag search.
To add a new statewide race: add to STATEWIDE_SERIES.
To add a new multi-district series: add to MULTI_DISTRICT_SERIES with a
  district filter function (see KXHOUSERACE for the pattern).
"""

import csv
import json
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# One row per series — statewide primaries and district-specific generals
# Note: Kalshi uses inconsistent naming. Most House generals are in KXHOUSERACE
# (national series, filtered to -MI- tickers). Some districts get their own
# dedicated series (e.g. HOUSEMI10). Check kalshi.com when new races appear.
STATEWIDE_SERIES = {
    "KXSENATEMID": "Michigan Senate — Dem Primary",
    "KXSENATEMIR": "Michigan Senate — Rep Primary",
    "KXGOVMINOMD": "Michigan Governor — Dem Primary",
    "KXGOVMINOMR": "Michigan Governor — Rep Primary",
    "HOUSEMI10":   "Michigan MI-10 House — General",
}

# Series that span many districts — filtered and split into one row per district
MULTI_DISTRICT_SERIES = {
    "KXHOUSERACE": {
        "filter": lambda ticker: bool(re.search(r"-MI\d{2}-", ticker)),
        "district_key": lambda ticker: re.search(r"-(MI\d{2})-", ticker).group(1),
        "label": lambda district: f"Michigan {district} House — General",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def get(path: str, params: dict = None) -> dict:
    url = f"{KALSHI_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 10 * (2 ** attempt)
                print(f"  Rate limited — waiting {wait}s...")
                time.sleep(wait)
            else:
                raise


def fetch_all_markets(series_ticker: str) -> list[dict]:
    markets, cursor = [], ""
    while True:
        params = {"series_ticker": series_ticker, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get("/markets", params)
        page = data.get("markets", [])
        markets.extend(page)
        cursor = data.get("cursor", "")
        if not cursor or len(page) < 200:
            break
        time.sleep(0.1)
    return markets


def safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def markets_to_row(label: str, series_ticker: str, markets: list[dict], pulled_at: str, max_candidates: int) -> dict:
    candidates = []
    total_volume = 0.0
    total_open_interest = 0.0

    for mkt in markets:
        if mkt.get("status") not in ("active", "open"):
            continue

        bid = safe_float(mkt.get("yes_bid_dollars")) or 0.0
        ask = safe_float(mkt.get("yes_ask_dollars")) or 0.0
        mid = round((bid + ask) / 2, 4) if (bid or ask) else None
        vol = safe_float(mkt.get("volume_fp")) or 0.0
        oi = safe_float(mkt.get("open_interest_fp")) or 0.0

        total_volume += vol
        total_open_interest += oi

        candidates.append({
            "name": mkt.get("yes_sub_title") or mkt.get("title", mkt.get("ticker", "")),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "volume_usd": vol,
            "open_interest_usd": oi,
        })

    candidates.sort(key=lambda c: c["mid"] or 0, reverse=True)
    expiration = markets[0].get("expiration_time", "") if markets else ""

    row = {
        "pulled_at": pulled_at,
        "series_ticker": series_ticker,
        "race": label,
        "expiration_date": expiration,
        "total_volume_usd": round(total_volume, 2),
        "total_open_interest_usd": round(total_open_interest, 2),
        "named_candidates": len(candidates),
    }

    for i in range(max_candidates):
        n = i + 1
        if i < len(candidates):
            c = candidates[i]
            row[f"candidate_{n}"] = c["name"]
            row[f"bid_{n}"] = f"{c['bid']:.3f}"
            row[f"ask_{n}"] = f"{c['ask']:.3f}"
            row[f"mid_{n}"] = f"{c['mid']:.3f}" if c["mid"] is not None else ""
            row[f"vol_{n}_usd"] = round(c["volume_usd"], 2)
            row[f"oi_{n}_usd"] = round(c["open_interest_usd"], 2)
        else:
            for col in (f"candidate_{n}", f"bid_{n}", f"ask_{n}", f"mid_{n}", f"vol_{n}_usd", f"oi_{n}_usd"):
                row[col] = ""

    return row


def main():
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Pulling Michigan Kalshi data at {pulled_at}")

    # (label, series_ticker, [markets]) tuples — one per row in the CSV
    race_groups: list[tuple[str, str, list[dict]]] = []

    # Statewide series — one row each
    for ticker, label in STATEWIDE_SERIES.items():
        mkts = fetch_all_markets(ticker)
        race_groups.append((label, ticker, mkts))
        print(f"  {label}: {len(mkts)} markets")

    # Multi-district series — split into one row per district
    for series_ticker, config in MULTI_DISTRICT_SERIES.items():
        all_mkts = fetch_all_markets(series_ticker)
        mi_mkts = [m for m in all_mkts if config["filter"](m.get("ticker", ""))]
        print(f"  {series_ticker}: {len(all_mkts)} total, {len(mi_mkts)} Michigan")

        by_district: dict[str, list[dict]] = defaultdict(list)
        for m in mi_mkts:
            district = config["district_key"](m.get("ticker", ""))
            by_district[district].append(m)

        for district in sorted(by_district):
            label = config["label"](district)
            race_groups.append((label, series_ticker, by_district[district]))

    if not race_groups:
        print("No races found.")
        return

    max_candidates = max(len(mkts) for _, _, mkts in race_groups)

    rows = [
        markets_to_row(label, ticker, mkts, pulled_at, max_candidates)
        for label, ticker, mkts in race_groups
    ]
    rows.sort(key=lambda r: r["total_volume_usd"], reverse=True)

    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"kalshi_snapshot_{ts}.csv"

    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    total_vol = sum(r["total_volume_usd"] for r in rows)
    total_oi = sum(r["total_open_interest_usd"] for r in rows)

    print(f"\nSaved: {out_path}\n")
    print(f"{'Race':<45} {'Volume':>10}  {'Open Interest':>13}  {'Leader'}")
    print("-" * 100)
    for r in rows:
        vol = f"${r['total_volume_usd']:,.0f}"
        oi = f"${r['total_open_interest_usd']:,.0f}"
        leader = f"{r.get('candidate_1', '—')} ({r.get('mid_1', '')})"
        print(f"  {r['race']:<43} {vol:>10}  {oi:>13}  {leader}")
    print("-" * 100)
    print(f"  {'TOTAL':<43} ${total_vol:>9,.0f}  ${total_oi:>12,.0f}")

    return {
        "races": len(rows),
        "volume_usd": round(total_vol, 2),
        "open_interest_usd": round(total_oi, 2),
    }


if __name__ == "__main__":
    main()
