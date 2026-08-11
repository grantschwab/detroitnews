"""
general_election.py

Post-primary general-election spending analysis for five head-to-head MI
races (Senate, 01, 04, 07, 10 -- 11/13 excluded as safely Democratic),
pushed to the same spreadsheet as the (now frozen) primary tabs
(HOUSE_SHEET_ID) under new tab names so the primary-era snapshot stays
untouched.

Unlike house_primaries.py's N-candidate Democratic primaries, every race
here is a genuine 2-candidate (R vs. D) matchup, so this module is
simpler and does not carry house_primaries.py's editorial exclusion list
or its since-July-1 primary-specific verification logic.

Reads only from already-compiled data (outside_spending_2026.csv for the
outside-money side, campaign_finance_2026_Q2.csv for cycle-to-date
campaign spend -- the pre-primary 12-day report this data's primary-era
counterpart used was a one-time filing tied to the primary and won't
recur) -- no new API calls except the one-time candidate last-name
resolution, which is cached. Called at the end of outside_spending.py's
own sheet-upload step only (not monitor_preprimary.py, whose own purpose
-- the 12-day pre-primary report -- ended with the primary).
"""

import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from groupspend import format_group_name, _write_sheet

RACE_CANDIDATES = {
    "mi00": ["elsayed", "rogers"],
    "mi01": ["bergman", "barr"],
    "mi04": ["huizenga", "mccann"],
    "mi07": ["barrett", "lawrence"],
    "mi10": ["bouchard", "hines"],
}
ALL_SLUGS = {slug for slugs in RACE_CANDIDATES.values() for slug in slugs}
SLUG_TO_RACE = {slug: r for r, slugs in RACE_CANDIDATES.items() for slug in slugs}

HOUSE_SHEET_ID = "1sNSKOIxYzR7XrgbPCFPwfM5VwuBNkORpMqSkj5oE3j4"
CANDIDATES_CSV = os.path.join(os.path.dirname(__file__), "candidates_general.csv")

BASE_URL = "https://api.open.fec.gov/v1"

# FEC candidate names are "LAST, FIRST" in ALL CAPS; .title()/.capitalize()
# mangles a few real surnames. Seeded defensively; verify against the live
# resolved cache (.general_candidate_names.json) if a name looks wrong.
LAST_NAME_OVERRIDES = {"mccann": "McCann", "elsayed": "El-Sayed"}


def _race_label(contest_id):
    return "Senate" if contest_id == "mi00" else "MI-" + contest_id[2:].upper()


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _resolve_last_names(cache_path):
    """slug -> proper display last name, resolved via FEC API once and
    cached (same pattern as house_primaries.py's own resolver)."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            cache = json.load(f)

    committee_ids = {}
    with open(CANDIDATES_CSV, newline="", encoding="utf-8") as f:
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
            name = data["results"][0]["name"]  # "LAST, FIRST"
            last = name.split(",")[0].strip()
            cache[slug] = last.capitalize()
        except Exception:
            cache[slug] = slug.capitalize()  # fallback, shouldn't normally hit

    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)
    return cache


def _outside_rows(output_dir):
    """List of dicts: slug, group, direction (Support/Oppose), all (SUM
    CandCategory), since_aug5 (Since Aug 5 Spent) -- one per (candidate,
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
                "since_aug5": _to_float(row.get("Since Aug 5 Spent")),
            })
    return rows


def _campaign_all_cycle(output_dir):
    """slug -> cycle-to-date C Expenditures, from the latest quarterly
    filing. The pre-primary 12-day report house_primaries.py used for
    this is a one-time filing tied to the primary and won't recur."""
    path = os.path.join(output_dir, "output", "campaign_finance_2026_Q2.csv")
    result = {slug: 0.0 for slug in ALL_SLUGS}
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            slug = row.get("Candidate Name", "").strip().lower()
            if slug in result:
                result[slug] = _to_float(row.get("C Expenditures"))
    return result


def _build_data(output_dir):
    cache_path = os.path.join(output_dir, ".general_candidate_names.json")
    last_names = _resolve_last_names(cache_path)
    outside = _outside_rows(output_dir)
    campaign_all = _campaign_all_cycle(output_dir)
    return last_names, outside, campaign_all


def _race_summary_label(contest_id, slugs, last_names):
    ordered_slugs = sorted(slugs, key=lambda s: last_names.get(s, s))
    names = ", ".join(last_names.get(s, s.capitalize()) for s in ordered_slugs)
    return f"{_race_label(contest_id)} ({names})"


def build_district_summary_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all = _build_data(output_dir)

    rows = []
    for contest_id, slugs in RACE_CANDIDATES.items():
        outside_all = sum(r["all"] for r in outside if r["slug"] in slugs)
        outside_aug5 = sum(r["since_aug5"] for r in outside if r["slug"] in slugs)
        camp_all = sum(campaign_all[slug] for slug in slugs)
        race = _race_summary_label(contest_id, slugs, last_names)
        rows.append({"District": race, "Time": "Since Aug 5", "Outside Spending": outside_aug5,
                     "Campaigns": "", "Total": outside_aug5})
        rows.append({"District": race, "Time": "Whole cycle", "Outside Spending": outside_all,
                     "Campaigns": camp_all, "Total": outside_all + camp_all})
    return rows


def build_candidate_position_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all = _build_data(output_dir)

    by_slug_direction = {}
    for r in outside:
        key = (r["slug"], r["direction"])
        by_slug_direction[key] = by_slug_direction.get(key, 0.0) + r["since_aug5"]

    rows = []
    for contest_id, slugs in RACE_CANDIDATES.items():
        race = _race_label(contest_id)
        race_rows = []
        for slug in slugs:
            last = last_names.get(slug, slug.capitalize())
            support = by_slug_direction.get((slug, "Support"), 0.0)
            # Oppose spending shown as negative, same convention as
            # house_primaries.py -- net outside-money picture reads
            # directly off Support/Oppose together.
            oppose = -by_slug_direction.get((slug, "Oppose"), 0.0)
            campaign = campaign_all[slug]
            total = support + oppose + campaign
            race_rows.append({"District": race, "Candidate": f"{last} ({race})",
                              "Outside support": support, "Outside opposition": oppose, "Campaign": campaign,
                              "_abs_total": abs(total)})
        race_rows.sort(key=lambda r: -r["_abs_total"])
        for r in race_rows:
            del r["_abs_total"]
        rows.extend(race_rows)
    return rows


def build_group_detail_rows(output_dir, credentials_path=None):
    last_names, outside, campaign_all = _build_data(output_dir)

    rows = []
    for contest_id, slugs in RACE_CANDIDATES.items():
        race = _race_label(contest_id)
        for slug in slugs:
            spend = campaign_all[slug]
            if spend > 0:
                last = last_names.get(slug, slug.capitalize())
                rows.append({"Group": f"{last} campaign", "District": race,
                             "Candidate": last, "Position": "Campaign", "Total": spend})
    for r in outside:
        if r["all"] <= 0:
            continue
        race = _race_label(SLUG_TO_RACE[r["slug"]])
        last = last_names.get(r["slug"], r["slug"].capitalize())
        rows.append({"Group": format_group_name(r["group"]), "District": race,
                     "Candidate": last, "Position": r["direction"], "Total": r["all"]})

    rows.sort(key=lambda r: -r["Total"])
    return rows


def update_district_summary(output_dir, credentials_path, worksheet_name="gen_district_overview"):
    rows = build_district_summary_rows(output_dir, credentials_path)
    columns = ["District", "Time", "Outside Spending", "Campaigns", "Total"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_candidate_positions(output_dir, credentials_path, worksheet_name="gen_candidate_overview"):
    rows = build_candidate_position_rows(output_dir, credentials_path)
    columns = ["District", "Candidate", "Outside support", "Outside opposition", "Campaign"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_group_detail(output_dir, credentials_path, worksheet_name="gen_group_detail"):
    rows = build_group_detail_rows(output_dir, credentials_path)
    columns = ["Group", "District", "Candidate", "Position", "Total"]
    _write_sheet(rows, columns, HOUSE_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_all(output_dir, credentials_path):
    update_district_summary(output_dir, credentials_path)
    update_candidate_positions(output_dir, credentials_path)
    update_group_detail(output_dir, credentials_path)
