"""
Pulls Michigan election markets from Polymarket's Gamma API.
Outputs a timestamped CSV with one row per race, candidates in wide format.

Run: python polymarket_mi.py
Output: data/snapshot_YYYYMMDD_HHMMSS.csv
"""

import csv
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAMMA_BASE = "https://gamma-api.polymarket.com"

MICHIGAN_TAG_IDS = [
    1433,   # Michigan Primary
    104024, # Michigan Midterm (general elections)
]

# Keyword filter on title/slug for races the tag search misses
MICHIGAN_KEYWORDS = re.compile(r"\bmi(?:chigan|-\d+)\b", re.IGNORECASE)

# Polymarket placeholder slots before real candidates/parties file
PLACEHOLDER_PATTERN = re.compile(
    r"^(person [a-z]|option [a-z]|another candidate|any other (person|candidate)|other)$",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}


def get(path: str, params: dict = None) -> list | dict:
    url = f"{GAMMA_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def fetch_events_by_tag(tag_id: int) -> list[dict]:
    events, limit, offset = [], 100, 0
    while True:
        page = get("/events", {"tag_id": tag_id, "limit": limit, "offset": offset})
        if not page:
            break
        events.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(0.25)
    return events


def fetch_events_by_keyword(keyword: str) -> list[dict]:
    results = get("/events", {"q": keyword, "limit": 100})
    return results if isinstance(results, list) else []


def is_michigan(event: dict) -> bool:
    return bool(
        MICHIGAN_KEYWORDS.search(event.get("title", ""))
        or MICHIGAN_KEYWORDS.search(event.get("slug", ""))
    )


def parse_candidate(question: str) -> str:
    # Primary format: "Will [Name] win/be the..."
    m = re.match(r"will (.+?) (?:be|win)\b", question, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # General election format: "[Party/Name] wins/win [race]"
    m = re.match(r"^(.+?)\s+wins?\b", question, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return question


def safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def event_to_row(event: dict, pulled_at: str, max_candidates: int) -> dict:
    """Flatten one event into a single wide-format row."""
    # Parse all named (non-placeholder) candidates, sorted by probability desc
    candidates = []
    placeholder_count = 0
    total_market_volume = 0.0

    for mkt in event.get("markets", []):
        question = mkt.get("question", "")
        name = parse_candidate(question)

        prices_raw = mkt.get("outcomePrices", [])
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except json.JSONDecodeError:
                prices_raw = []

        outcomes = mkt.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = []
        price_map = dict(zip(outcomes, prices_raw))
        prob = safe_float(price_map.get("Yes"))

        mkt_vol = safe_float(mkt.get("volume")) or 0.0
        total_market_volume += mkt_vol

        if PLACEHOLDER_PATTERN.match(name):
            placeholder_count += 1
        else:
            candidates.append({
                "name": name,
                "prob": prob,
                "volume_usd": mkt_vol,
                "market_id": mkt.get("id", ""),
                "condition_id": mkt.get("conditionId", ""),
            })

    candidates.sort(key=lambda c: c["prob"] or 0, reverse=True)

    row = {
        "pulled_at": pulled_at,
        "event_id": event.get("id", ""),
        "event_title": event.get("title", ""),
        "event_slug": event.get("slug", ""),
        "event_end_date": event.get("endDate", ""),
        "event_closed": event.get("closed", False),
        # Total volume across all candidate markets within this event
        "total_market_volume_usd": round(total_market_volume, 2),
        # Event-level volume from API (may differ — Polymarket counts differently)
        "event_volume_usd": safe_float(event.get("volume")),
        "event_liquidity_usd": safe_float(event.get("liquidityAmm") or event.get("liquidity")),
        "event_open_interest_usd": safe_float(event.get("openInterest")),
        "named_candidates": len(candidates),
        "placeholder_slots": placeholder_count,
    }

    # Wide columns: candidate_1 / prob_1 / vol_1 ... up to max_candidates
    for i in range(max_candidates):
        n = i + 1
        if i < len(candidates):
            c = candidates[i]
            row[f"candidate_{n}"] = c["name"]
            row[f"prob_{n}"] = f"{c['prob']:.3f}" if c["prob"] is not None else ""
            row[f"vol_{n}_usd"] = round(c["volume_usd"], 2)
        else:
            row[f"candidate_{n}"] = ""
            row[f"prob_{n}"] = ""
            row[f"vol_{n}_usd"] = ""

    return row


def main():
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Pulling Michigan Polymarket data at {pulled_at}")

    seen_ids: set = set()
    all_events: list[dict] = []

    for tag_id in MICHIGAN_TAG_IDS:
        for evt in fetch_events_by_tag(tag_id):
            if evt["id"] not in seen_ids:
                seen_ids.add(evt["id"])
                all_events.append(evt)

    for kw in ["Michigan", "MI-10", "MI-11", "MI-12", "MI-13", "MI-14"]:
        for evt in fetch_events_by_keyword(kw):
            if evt["id"] not in seen_ids and is_michigan(evt):
                seen_ids.add(evt["id"])
                all_events.append(evt)

    print(f"Found {len(all_events)} Michigan events")

    if not all_events:
        print("No events found.")
        return

    # Find the max named candidate count so we know how many wide columns to make
    max_candidates = 0
    for evt in all_events:
        named = sum(
            1 for mkt in evt.get("markets", [])
            if not PLACEHOLDER_PATTERN.match(parse_candidate(mkt.get("question", "")))
        )
        max_candidates = max(max_candidates, named)

    rows = [event_to_row(evt, pulled_at, max_candidates) for evt in all_events]

    # Sort by event_volume_usd descending so biggest races are at top
    rows.sort(key=lambda r: r["event_volume_usd"] or 0, reverse=True)

    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"polymarket_snapshot_{ts}.csv"

    fieldnames = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    total_vol = sum(r["event_volume_usd"] or 0 for r in rows)
    total_liquidity = sum(r["event_liquidity_usd"] or 0 for r in rows)
    total_open_interest = sum(r["event_open_interest_usd"] or 0 for r in rows)

    print(f"Saved: {out_path}\n")
    print(f"{'Race':<55} {'Volume':>12}  {'Top candidate'}")
    print("-" * 85)
    for r in rows:
        vol = f"${r['event_volume_usd']:,.0f}" if r["event_volume_usd"] else "n/a"
        top = f"{r.get('candidate_1', '')} ({r.get('prob_1', '')})" if r.get("candidate_1") else "—"
        print(f"  {r['event_title']:<53} {vol:>12}  {top}")
    print("-" * 85)
    print(f"  {'TOTAL across all races':<53} ${total_vol:>11,.0f}")
    print(f"  {'Total liquidity':<53} ${total_liquidity:>11,.0f}")

    return {
        "races": len(rows),
        "volume_usd": round(total_vol, 2),
        "liquidity_usd": round(total_liquidity, 2),
        "open_interest_usd": round(total_open_interest, 2),
    }


if __name__ == "__main__":
    main()
