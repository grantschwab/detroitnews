"""
house_primaries.py

Democratic primary spending analysis for four competitive MI House
districts (07, 10, 11, 13), pushed to a dedicated spreadsheet
(HOUSE_SHEET_ID) separate from the Senate race's graphics sheet.

Unlike the Senate race, these are N-candidate primaries (not
head-to-head), so this reuses groupspend.py's format_group_name()/
_write_sheet() utilities but implements its own N-candidate-aware
aggregation rather than extending overallspend.py/groupspend.py's
2-candidate logic.

Reads only from already-compiled/downloaded data (campaign_finance_2026_
preprimary.csv, outside_spending_2026.csv, and the raw F3N/F3A filings
under raw_preprimary/ for since-July-1 verification) -- no new API calls
except the one-time candidate last-name resolution, which is cached.
Called at the end of outside_spending.py's and monitor_preprimary.py's
own sheet-upload steps.
"""

import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from groupspend import format_group_name, _write_sheet

# Editorial call (2026-07-30): only the candidates Grant considers live
# contenders in each primary. Others (Jaye, Busch, Adams in MI-10; Cowen,
# Rais, Badger in MI-07; Baker in MI-11; Hollier, Hawkins in MI-13) are
# deliberately excluded as minor/dropped-out, not because they lack data
# -- so don't auto-derive this list from candidates.csv.
DISTRICT_CANDIDATES = {
    "mi07": ["brink", "maasdam", "lawrence"],
    "mi10": ["hines", "greimel", "chung"],
    "mi11": ["moss", "farooqi", "ufford"],
    "mi13": ["mckinney", "thanedar"],
}
ALL_SLUGS = {slug for slugs in DISTRICT_CANDIDATES.values() for slug in slugs}
SLUG_TO_DISTRICT = {slug: d for d, slugs in DISTRICT_CANDIDATES.items() for slug in slugs}

HOUSE_SHEET_ID = "1sNSKOIxYzR7XrgbPCFPwfM5VwuBNkORpMqSkj5oE3j4"

BASE_URL = "https://api.open.fec.gov/v1"

# FEC candidate names are "LAST, FIRST" in ALL CAPS; .title() mangles a
# few real surnames (e.g. "Mckinney" instead of "McKinney"). Small fixed
# roster, so a manual override is simpler and more reliable than generic
# capitalization heuristics.
LAST_NAME_OVERRIDES = {"mckinney": "McKinney"}


def _district_label(contest_id):
    return "MI-" + contest_id[2:].upper()


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_last_names(candidates_csv_path, cache_path):
    """slug -> proper display last name, resolved via FEC API once and
    cached (same pattern as groupspend.py's committee-name cache)."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)

    committee_ids = {}
    with open(candidates_csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row["candidate_name"]
            if slug in ALL_SLUGS:
                committee_ids[slug] = row["committee_id"]

    api_key = os.environ.get("FEC_API_KEY", "")
    for slug, committee_id in committee_ids.items():
        if slug in cache:
            continue
        if slug in LAST_NAME_OVERRIDES:
            cache[slug] = LAST_NAME_OVERRIDES[slug]
            continue
        try:
            params = {"api_key": api_key}
            url = f"{BASE_URL}/committee/{committee_id}/candidates/?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read())
            name = data["results"][0]["name"]  # "LAST,, FIRST" or "LAST, FIRST"
            last = name.split(",")[0].strip()
            cache[slug] = last.capitalize()
        except Exception:
            cache[slug] = slug.capitalize()  # fallback, shouldn't normally hit

    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)
    return cache


def _outside_rows(output_dir):
    """List of dicts: slug, group, direction (Support/Oppose), all (SUM
    CandCategory), since_july1 (Since Jul 1 Spent) -- one per (candidate,
    group, direction) already present in outside_spending_2026.csv."""
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row.get("Candidate Name", "").strip().lower()
            if slug not in ALL_SLUGS:
                continue
            rows.append({
                "slug": slug,
                "group": row.get("Outside Group", "").strip(),
                "direction": row.get("Support/Oppose", "").strip(),
                "all": _to_float(row.get("SUM CandCategory")),
                "since_july1": _to_float(row.get("Since Jul 1 Spent")),
            })
    return rows


def _campaign_all_cycle(output_dir):
    """slug -> cycle-to-date C Expenditures, from the pre-primary filing."""
    path = os.path.join(output_dir, "output", "campaign_finance_2026_preprimary.csv")
    result = {slug: 0.0 for slug in ALL_SLUGS}
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row.get("Candidate Name", "").strip().lower()
            if slug in result:
                result[slug] = _to_float(row.get("C Expenditures"))
    return result


def _campaign_since_july1(output_dir, state_path):
    """slug -> verified since-July-1 campaign spend (Column A "this
    period" operating expenditures from the pre-primary filing), but
    ONLY when that filing's own coverage_from_date is genuinely 2026-07-01
    -- guards against a stale/wrong-window filing (e.g. a candidate whose
    raw data is still a leftover 2024-cycle report) silently mislabeling
    the wrong window as "since July 1"."""
    result = {slug: 0.0 for slug in ALL_SLUGS}
    if not os.path.exists(state_path):
        return result
    with open(state_path) as f:
        state = json.load(f)

    with open(os.path.join(os.path.dirname(__file__), "candidates.csv"),
              newline="", encoding="utf-8") as f:
        committee_by_slug = {row["candidate_name"]: row for row in csv.DictReader(f)
                              if row["candidate_name"] in ALL_SLUGS}

    output_dir_raw = os.path.dirname(state_path)  # ".." from code/, i.e. FEC_071526/
    for slug, cand in committee_by_slug.items():
        committee_id = cand["committee_id"]
        s = state.get(committee_id, {})
        file_number = s.get("file_number")
        if not file_number:
            continue
        contest_id = cand["contest_id"]
        candidate_dir = os.path.join(output_dir_raw, "raw_preprimary", contest_id,
                                      cand.get("party", "DEM"), slug, str(file_number))
        for fname in ("F3N.csv", "F3A.csv"):
            fpath = os.path.join(candidate_dir, fname)
            if not os.path.exists(fpath):
                continue
            with open(fpath, newline="", encoding="utf-8") as f:
                row = next(csv.DictReader(f), None)
            if row and row.get("coverage_from_date") == "2026-07-01":
                result[slug] = _to_float(row.get("col_a_total_operating_expenditures"))
            else:
                print(f"  house_primaries: {slug} coverage_from_date != 2026-07-01 "
                      f"(got {row.get('coverage_from_date') if row else 'no data'}), "
                      f"since-July-1 campaign spend left at $0")
            break
    return result


def _build_data(output_dir, credentials_path):
    state_path = os.path.join(output_dir, ".monitor_preprimary_state.json")
    cache_path = os.path.join(output_dir, ".house_candidate_names.json")
    candidates_csv_path = os.path.join(os.path.dirname(__file__), "candidates.csv")

    last_names = _resolve_last_names(candidates_csv_path, cache_path)
    outside = _outside_rows(output_dir)
    campaign_all = _campaign_all_cycle(output_dir)
    campaign_july1 = _campaign_since_july1(output_dir, state_path)

    return last_names, outside, campaign_all, campaign_july1


def build_district_summary_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all, campaign_july1 = _build_data(output_dir, credentials_path)

    rows = []
    for contest_id, slugs in DISTRICT_CANDIDATES.items():
        outside_all = sum(r["all"] for r in outside if r["slug"] in slugs)
        outside_july1 = sum(r["since_july1"] for r in outside if r["slug"] in slugs)
        camp_all = sum(campaign_all[slug] for slug in slugs)
        camp_july1 = sum(campaign_july1[slug] for slug in slugs)
        rows.append({
            "District": _district_label(contest_id),
            "Total Outside Spending (All Cycle)": outside_all,
            "Total Outside Spending (Since Jul 1)": outside_july1,
            "Campaign (All)": camp_all,
            "Campaign (Jul 1)": camp_july1,
            "Total (All Cycle)": outside_all + camp_all,
            "Total (Since Jul 1)": outside_july1 + camp_july1,
        })
    return rows


def build_candidate_position_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all, campaign_july1 = _build_data(output_dir, credentials_path)

    # Sum outside spend per (slug, direction) across all groups first.
    by_slug_direction = {}
    for r in outside:
        key = (r["slug"], r["direction"])
        d = by_slug_direction.setdefault(key, {"all": 0.0, "since_july1": 0.0})
        d["all"] += r["all"]
        d["since_july1"] += r["since_july1"]

    rows = []
    for contest_id, slugs in DISTRICT_CANDIDATES.items():
        district = _district_label(contest_id)
        for slug in slugs:
            last = last_names.get(slug, slug.capitalize())
            camp_all = campaign_all[slug]
            camp_july1 = campaign_july1[slug]
            if camp_all > 0 or camp_july1 > 0:
                rows.append({"District": district, "Candidate": last, "Position": "campaign",
                             "All": camp_all, "Since_July1": camp_july1})
            for direction, position in (("Support", "support"), ("Oppose", "oppose")):
                d = by_slug_direction.get((slug, direction))
                if d and (d["all"] > 0 or d["since_july1"] > 0):
                    rows.append({"District": district, "Candidate": last, "Position": position,
                                 "All": d["all"], "Since_July1": d["since_july1"]})
    return rows


def build_group_detail_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all, campaign_july1 = _build_data(output_dir, credentials_path)

    rows = []
    for contest_id, slugs in DISTRICT_CANDIDATES.items():
        district = _district_label(contest_id)
        for slug in slugs:
            spend = campaign_all[slug]
            if spend > 0:
                last = last_names.get(slug, slug.capitalize())
                rows.append({"Group": f"{last} campaign", "District": district,
                             "Candidate": last, "Direction": "Campaign", "Total": spend})
    for r in outside:
        if r["all"] <= 0:
            continue
        district = _district_label(SLUG_TO_DISTRICT[r["slug"]])
        last = last_names.get(r["slug"], r["slug"].capitalize())
        rows.append({"Group": format_group_name(r["group"]), "District": district,
                     "Candidate": last, "Direction": r["direction"], "Total": r["all"]})

    rows.sort(key=lambda r: (r["District"], -r["Total"]))
    return rows


def update_district_summary(output_dir, credentials_path, worksheet_name="district_summary"):
    rows = build_district_summary_rows(output_dir, credentials_path)
    columns = ["District", "Total Outside Spending (All Cycle)", "Total Outside Spending (Since Jul 1)",
               "Campaign (All)", "Campaign (Jul 1)", "Total (All Cycle)", "Total (Since Jul 1)"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_candidate_positions(output_dir, credentials_path, worksheet_name="candidate_positions"):
    rows = build_candidate_position_rows(output_dir, credentials_path)
    columns = ["District", "Candidate", "Position", "All", "Since_July1"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_group_detail(output_dir, credentials_path, worksheet_name="group_detail"):
    rows = build_group_detail_rows(output_dir, credentials_path)
    columns = ["Group", "District", "Candidate", "Direction", "Total"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_all(output_dir, credentials_path):
    update_district_summary(output_dir, credentials_path)
    update_candidate_positions(output_dir, credentials_path)
    update_group_detail(output_dir, credentials_path)
