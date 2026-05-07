"""
Pulls both Polymarket and Kalshi Michigan election data and writes one combined
row to data/totals.csv.

Run: python run_all.py
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

import polymarket_mi
import kalshi_mi

def main():
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    pm = polymarket_mi.main()
    print()
    ka = kalshi_mi.main()

    if not pm or not ka:
        print("\nOne or both pulls returned no data — totals not written.")
        return

    combined_vol = round(pm["volume_usd"] + ka["volume_usd"], 2)
    combined_oi = round(pm["open_interest_usd"] + ka["open_interest_usd"], 2)

    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    totals_path = out_dir / "totals.csv"
    write_header = not totals_path.exists()

    fieldnames = [
        "pulled_at",
        "pm_races", "pm_volume_usd", "pm_liquidity_usd", "pm_open_interest_usd",
        "ka_races", "ka_volume_usd", "ka_open_interest_usd",
        "combined_volume_usd", "combined_open_interest_usd",
    ]

    with open(totals_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "pulled_at": pulled_at,
            "pm_races": pm["races"],
            "pm_volume_usd": pm["volume_usd"],
            "pm_liquidity_usd": pm["liquidity_usd"],
            "pm_open_interest_usd": pm["open_interest_usd"],
            "ka_races": ka["races"],
            "ka_volume_usd": ka["volume_usd"],
            "ka_open_interest_usd": ka["open_interest_usd"],
            "combined_volume_usd": combined_vol,
            "combined_open_interest_usd": combined_oi,
        })

    print(f"\n{'─' * 55}")
    print(f"  Polymarket   {pm['races']} races   ${pm['volume_usd']:>12,.0f}")
    print(f"  Kalshi       {ka['races']} races   ${ka['volume_usd']:>12,.0f}")
    print(f"  {'─' * 40}")
    print(f"  Combined                  ${combined_vol:>12,.0f}")
    print(f"  Open interest             ${combined_oi:>12,.0f}")
    print(f"  Appended to {totals_path.name}")

if __name__ == "__main__":
    main()
