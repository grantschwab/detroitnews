"""
coordinated_spend.py

Coordinated party expenditures (Schedule F) for MI Senate and House races, laid out
like outside_spend_gen so the two tabs can be read side by side in FEC_Michigan_2026.
One row per (party committee, candidate). Period columns bucket each expenditure by
its date: 2025, Q1 2026, Q2 2026, and since Aug 1 2026.

Uses the same committee list as party_coordinated.py. Kept separate from
outside_spending.py's independent expenditures so the two are never summed without a
label.
"""

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

from party_coordinated import PARTY_COMMITTEES, query_fec

SHEET_ID = "10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc"  # FEC_Michigan_2026
WORKSHEET = "coordinated_spend"
CYCLE = 2026
COMMITTEE_PARTY = {"DSCC": "DEM", "DCCC": "DEM", "Michigan Democratic Party": "DEM",
                    "NRSC": "REP", "NRCC": "REP", "Michigan Republican Party": "REP"}

PERIODS = [
    ("2025 Spent", "2025-01-01", "2025-12-31"),
    ("Q1 2026 Spent", "2026-01-01", "2026-03-31"),
    ("Q2 2026 Spent", "2026-04-01", "2026-06-30"),
    ("Since Aug 1 Spent", "2026-08-01", "2099-12-31"),
]
PERIOD_COLUMNS = [name for name, _, _ in PERIODS]
OUTPUT_COLUMNS = [
    "Candidate Name", "First Name", "Contest ID", "Party",
    "Support/Oppose", "Party Committee",
    *PERIOD_COLUMNS,
    "SUM CandCategory", "# Transactions", "Most Recent Filing",
    "FEC Committee Link",
]


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _schedule_f_rows(committee_id):
    rows = []
    last_index = None
    while True:
        params = {"committee_id": committee_id, "cycle": CYCLE, "per_page": 100}
        if last_index:
            params["last_index"] = last_index
        data = query_fec("schedules/schedule_f/", params)
        results = data.get("results", [])
        rows.extend(results)
        last_index = (data.get("pagination", {}).get("last_indexes") or {}).get("last_index")
        if not results or not last_index:
            break
        time.sleep(0.5)
    return rows


def _period_for(date):
    date = (date or "")[:10]
    for name, start, end in PERIODS:
        if start <= date <= end:
            return name
    return None


def build_rows():
    grouped = {}
    for party, committee_id in PARTY_COMMITTEES.items():
        for r in _schedule_f_rows(committee_id):
            state = (r.get("candidate_office_state") or "").upper()
            office = (r.get("candidate_office") or "").upper()
            if state != "MI" or office not in ("S", "H"):
                continue
            name = (r.get("candidate_name") or "").strip()
            last, _, first = name.partition(",")
            last, first = last.strip().title(), first.strip().split(" ")[0].title() if first else ""
            district = (r.get("candidate_office_district") or "").zfill(2) if office == "H" else ""
            contest = "mi00" if office == "S" else f"mi{district}"
            key = (party, last, first, contest)
            row = grouped.setdefault(key, {
                "Candidate Name": last.lower(),
                "First Name": first.lower(),
                "Contest ID": contest,
                "Party": COMMITTEE_PARTY.get(party, ""),
                "Support/Oppose": "Support",
                "Party Committee": party,
                **{p: 0.0 for p in PERIOD_COLUMNS},
                "SUM CandCategory": 0.0,
                "# Transactions": 0,
                "Most Recent Filing": "",
                "FEC Committee Link": f"https://www.fec.gov/data/committee/{committee_id}/",
            })
            amount = _to_float(r.get("expenditure_amount"))
            period = _period_for(r.get("expenditure_date"))
            if period:
                row[period] += amount
            row["SUM CandCategory"] += amount
            row["# Transactions"] += 1
            row["Most Recent Filing"] = max(row["Most Recent Filing"], (r.get("expenditure_date") or "")[:10])
    rows = list(grouped.values())
    for row in rows:
        for p in PERIOD_COLUMNS + ["SUM CandCategory"]:
            row[p] = round(row[p], 2)
    rows.sort(key=lambda r: -r["SUM CandCategory"])
    return rows


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def update(output_dir, credentials_path):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    rows = build_rows()
    write_csv(rows, os.path.join(output_dir, "output", "coordinated_spending_2026.csv"))

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SHEET_ID)
    try:
        ws = spreadsheet.worksheet(WORKSHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=WORKSHEET, rows=max(len(rows) + 10, 50), cols=len(OUTPUT_COLUMNS) + 2)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    last_col = chr(64 + len(OUTPUT_COLUMNS))
    ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    ws.format(f"G2:{last_col}{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})
    ws.freeze(cols=1)
    return rows


if __name__ == "__main__":
    for row in build_rows():
        print(row)
