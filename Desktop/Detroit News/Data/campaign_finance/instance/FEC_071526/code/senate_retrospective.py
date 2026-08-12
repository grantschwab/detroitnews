"""
senate_retrospective.py

One-off, run-by-hand look back at the Michigan Senate primaries (Dem and
GOP, both concluded Aug 4 2026) across all four notable candidates:
Haley Stevens, Abdul El-Sayed, Mallory McMorrow (Dem primary field) and
Mike Rogers (unopposed GOP nominee, a separate primary). Feeds two new
tabs in the Flourish-facing graphics spreadsheet.

Unlike the live pipeline, this is a genuine retrospective: outside
spending is capped at expenditure_date <= 2026-08-04 (primary day) so
Rogers' and El-Sayed's ongoing general-election activity (they're both
still tracked live under candidates_general.csv) doesn't leak into what's
supposed to be a primary-era snapshot.

SEN_retro_overall's "Campaign (ad-related spend)" column reads
output/senate_ad_spend_summary.csv, written by senate_ad_spend.py -- run
that script first (or after any change to its categorization logic) so
this column reflects current data. See GUIDE.md ("Senate Ad Spend
Categorization Methodology") for what counts as ad-related.

Not wired into any continuous polling loop -- run manually:

    export FEC_API_KEY="..."
    python senate_retrospective.py --credentials ../app-template-access-....json
"""

import argparse
import csv
import os

import outside_spending as osp
from groupspend import format_group_name, _write_sheet

CANDIDATES_CSV = os.path.join(os.path.dirname(__file__), "candidates.csv")
SLUGS = {"stevens", "elsayed", "mcmorrow", "rogers"}
PRIMARY_CUTOFF = "2026-08-04"

GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"

DISPLAY_NAME = {"stevens": "Stevens", "elsayed": "El-Sayed", "mcmorrow": "McMorrow", "rogers": "Rogers"}


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _load_candidates():
    with open(CANDIDATES_CSV, newline="", encoding="utf-8") as f:
        return {row["candidate_name"]: row for row in csv.DictReader(f) if row["candidate_name"] in SLUGS}


def _fetch_primary_transactions():
    """Raw, deduped Schedule E transactions for the MI Senate contest,
    capped at the primary-day cutoff."""
    raw = osp.fetch_schedule_e_for_contest("MI", "S", None, 2026, "2025-01-01")
    deduped = osp.dedupe_notice_vs_periodic(raw)
    return [t for t in deduped if (t.get("expenditure_date") or "") <= PRIMARY_CUTOFF]


def _campaign_spend():
    """C Expenditures per candidate, from the still-current pre-primary
    filing (monitor_preprimary.py never stopped tracking these 4, since
    it still runs against the original candidates.csv)."""
    path = os.path.join(os.path.dirname(__file__), "..", "output", "campaign_finance_2026_preprimary.csv")
    result = {slug: 0.0 for slug in SLUGS}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row.get("Candidate Name", "").strip().lower()
            if slug in result:
                result[slug] = _to_float(row.get("C Expenditures"))
    return result


def _ad_related_spend():
    """Per-candidate 'Total ad-related' from senate_ad_spend.py's saved
    summary -- see GUIDE.md ("Senate Ad Spend Categorization Methodology")
    for what counts as ad-related and the categorization's known limits.
    Requires senate_ad_spend.py to have been run at least once."""
    path = os.path.join(os.path.dirname(__file__), "..", "output", "senate_ad_spend_summary.csv")
    result = {slug: 0.0 for slug in SLUGS}
    by_display_name = {v: k for k, v in DISPLAY_NAME.items()}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = by_display_name.get(row.get("Candidate", "").strip())
            if slug in result:
                result[slug] = _to_float(row.get("Total ad-related"))
    return result


_DATA_CACHE = None


def build_data():
    """Memoized -- _fetch_primary_transactions() is a live API pull, and
    both build_overall_rows() and build_group_rows() need this data."""
    global _DATA_CACHE
    if _DATA_CACHE is not None:
        return _DATA_CACHE

    candidates = _load_candidates()
    transactions = _fetch_primary_transactions()
    campaign = _campaign_spend()
    ad_spend = _ad_related_spend()

    # (slug, direction) -> total, and (slug, group, direction) -> total
    by_slug_direction = {}
    by_group = {}
    for t in transactions:
        slug = osp.slugify(t.get("candidate_last_name"))
        if slug not in SLUGS:
            continue
        direction = "Support" if t.get("support_oppose_indicator") == "S" else "Oppose"
        amount = _to_float(t.get("expenditure_amount"))
        by_slug_direction[(slug, direction)] = by_slug_direction.get((slug, direction), 0.0) + amount
        group = (t.get("committee") or {}).get("name") or (t.get("committee") or {}).get("committee_id", "UNKNOWN")
        key = (slug, group, direction)
        by_group[key] = by_group.get(key, 0.0) + amount

    _DATA_CACHE = (candidates, by_slug_direction, by_group, campaign, ad_spend)
    return _DATA_CACHE


def build_overall_rows():
    candidates, by_slug_direction, _, campaign, ad_spend = build_data()
    rows = []
    for slug in ("stevens", "elsayed", "mcmorrow", "rogers"):
        support = by_slug_direction.get((slug, "Support"), 0.0)
        oppose = by_slug_direction.get((slug, "Oppose"), 0.0)
        camp = campaign[slug]
        rows.append({
            "Candidate": DISPLAY_NAME[slug],
            "Campaign": camp,
            "Campaign (ad-related spend)": ad_spend[slug],
            "Outside Support": support,
            "Outside Oppose": oppose,
            "Total": camp + support + oppose,
        })
    return rows


DEM_SLUGS = ("stevens", "elsayed", "mcmorrow")
ALL_SLUGS_ORDERED = ("stevens", "elsayed", "mcmorrow", "rogers")


def build_v2_rows():
    """Two-row version matching SEN_overall_chart's shape: one row for
    all three Dem primary candidates combined ("Democrats and
    supporters"), one for Rogers. Per Grant 2026-08-12: Campaign columns
    use full campaign spend (not just the ad-related subset). Pro/Anti
    are broken out PER CANDIDATE (Pro-Stevens, Anti-Stevens, Pro-El-Sayed,
    Anti-El-Sayed, ...), not summed across the Dem side -- an earlier
    version of this function summed them into one Pro-candidate/
    Anti-candidate pair per row, which silently conflated intra-party
    dynamics (a group Pro-Stevens/Anti-El-Sayed would count toward both
    columns as if it were pro/anti the whole Dem field). Per-candidate
    columns avoid that ambiguity entirely -- each candidate's own number
    stays its own number, regardless of which row it's grouped under."""
    candidates, by_slug_direction, _, campaign, _ = build_data()

    row_dem = {"Category": "Democrats and supporters"}
    row_rogers = {"Category": "Rogers and supporters"}
    for slug in ALL_SLUGS_ORDERED:
        row = row_dem if slug in DEM_SLUGS else row_rogers
        other = row_rogers if slug in DEM_SLUGS else row_dem
        name = DISPLAY_NAME[slug]
        row[f"{name} campaign"] = campaign[slug]
        row[f"Pro-{name}"] = by_slug_direction.get((slug, "Support"), 0.0)
        row[f"Anti-{name}"] = by_slug_direction.get((slug, "Oppose"), 0.0)
        other[f"{name} campaign"] = 0.0
        other[f"Pro-{name}"] = 0.0
        other[f"Anti-{name}"] = 0.0

    value_cols = [c for slug in ALL_SLUGS_ORDERED for c in
                  (f"{DISPLAY_NAME[slug]} campaign", f"Pro-{DISPLAY_NAME[slug]}", f"Anti-{DISPLAY_NAME[slug]}")]
    for row in (row_dem, row_rogers):
        row["Total"] = sum(row[c] for c in value_cols)

    return [row_dem, row_rogers]


def update_v2(credentials_path, worksheet_name="SEN_retro_v2"):
    rows = build_v2_rows()
    columns = ["Category"]
    for slug in ALL_SLUGS_ORDERED:
        name = DISPLAY_NAME[slug]
        columns += [f"{name} campaign", f"Pro-{name}", f"Anti-{name}"]
    columns.append("Total")
    _write_sheet(rows, columns, GRAPHICS_SHEET_ID, credentials_path, worksheet_name)
    return rows


def build_group_rows():
    _, _, by_group, _, _ = build_data()
    rows = []
    for (slug, group, direction), total in by_group.items():
        if total <= 0:
            continue
        rows.append({
            "Group": format_group_name(group),
            "Candidate": DISPLAY_NAME[slug],
            "Position": direction,
            "Total": total,
        })
    rows.sort(key=lambda r: -r["Total"])
    return rows


def update_overall(credentials_path, worksheet_name="SEN_retro_overall"):
    rows = build_overall_rows()
    columns = ["Candidate", "Campaign", "Campaign (ad-related spend)", "Outside Support", "Outside Oppose", "Total"]
    _write_sheet(rows, columns, GRAPHICS_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_groups(credentials_path, worksheet_name="SEN_retro_groups"):
    rows = build_group_rows()
    columns = ["Group", "Candidate", "Position", "Total"]
    _write_sheet(rows, columns, GRAPHICS_SHEET_ID, credentials_path, worksheet_name)
    return rows


def main():
    parser = argparse.ArgumentParser(description="Senate primary retrospective (one-off, run by hand).")
    parser.add_argument("--credentials", default=os.path.join(
        os.path.dirname(__file__), "..", "app-template-access-402821-3111eabfc82d.json"))
    parser.add_argument("--dry-run", action="store_true", help="Print tables, don't push to Sheets.")
    args = parser.parse_args()

    overall = build_overall_rows()
    print("=== SEN_retro_overall ===")
    for r in overall:
        print(f"  {r['Candidate']:<10} Campaign={r['Campaign']:>14,.0f} "
              f"AdSpend={r['Campaign (ad-related spend)']:>14,.0f} "
              f"Support={r['Outside Support']:>14,.0f} Oppose={r['Outside Oppose']:>14,.0f} "
              f"Total={r['Total']:>14,.0f}")

    groups = build_group_rows()
    print(f"\n=== SEN_retro_groups ({len(groups)} rows) ===")
    for r in groups[:15]:
        print(f"  {r['Group'][:40]:<40} {r['Candidate']:<10} {r['Position']:<8} {r['Total']:>14,.0f}")

    v2 = build_v2_rows()
    print("\n=== SEN_retro_v2 ===")
    for r in v2:
        print(f"  {r}")

    if args.dry_run:
        print("\n--dry-run: not pushing to Sheets.")
        return

    update_overall(args.credentials)
    print("\nSEN_retro_overall updated.")
    update_groups(args.credentials)
    print("SEN_retro_groups updated.")
    update_v2(args.credentials)
    print("SEN_retro_v2 updated.")


if __name__ == "__main__":
    main()
