"""
party_coordinated.py

Coordinated party expenditures (FEC Schedule F) in the MI Senate race, from the
national and state party committees that make them. Kept separate from
outside_spending.py's independent expenditures (Schedule E) so the two are never
summed without a label.

Party committees: DSCC, NRSC, DCCC, NRCC, Michigan Democratic Party, Michigan Republican
Party. Covers MI Senate (candidate_office "S") and MI House (candidate_office "H"); the
Race column distinguishes them. Note: the NRSC main committee is C00027466 (C00091009 is a contributions account with no Schedule F). The NRCC's main committee is C00075820 (its
C00002931 account carries no Schedule F).

Wired into outside_spending.py's live loop, writing the "SEN_party_coordinated" tab.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

BASE_URL = "https://api.open.fec.gov/v1"
GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"
PARTY_COMMITTEES = {
    "DSCC": "C00042366",
    "NRSC": "C00027466",
    "DCCC": "C00000935",
    "NRCC": "C00075820",
    "Michigan Democratic Party": "C00031054",
    "Michigan Republican Party": "C00041160",
}
OUTPUT_COLUMNS = ["Party committee", "Race", "Candidate", "Coordinated spending"]


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def query_fec(endpoint, params, retries=6):
    params = dict(params)
    params["api_key"] = os.environ.get("FEC_API_KEY", "")
    url = f"{BASE_URL}/{endpoint}?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(min(3 * (2 ** attempt), 30))
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise
    return {}


def _schedule_f_rows(committee_id, cycle=2026):
    rows = []
    last_index = None
    while True:
        params = {"committee_id": committee_id, "cycle": cycle, "per_page": 100}
        if last_index:
            params["last_index"] = last_index
        data = query_fec("schedules/schedule_f/", params)
        results = data.get("results", [])
        rows.extend(results)
        pagination = data.get("pagination", {})
        last_index = (pagination.get("last_indexes") or {}).get("last_index")
        if not results or not last_index:
            break
    return rows


def build_rows():
    """One row per (party committee, candidate) with summed MI Senate coordinated spending."""
    totals = defaultdict(float)
    for party, committee_id in PARTY_COMMITTEES.items():
        for r in _schedule_f_rows(committee_id):
            if (r.get("candidate_office_state") or "").upper() != "MI":
                continue
            office = (r.get("candidate_office") or "").upper()
            if office not in ("S", "H"):
                continue
            race = "Senate" if office == "S" else f"MI-{(r.get('candidate_office_district') or '').zfill(2)}"
            candidate = (r.get("candidate_name") or "").strip().title()
            totals[(party, race, candidate)] += _to_float(r.get("expenditure_amount"))
    rows = [
        {"Party committee": party, "Race": race, "Candidate": candidate, "Coordinated spending": round(amount, 2)}
        for (party, race, candidate), amount in totals.items()
    ]
    rows.sort(key=lambda r: -r["Coordinated spending"])
    return rows


def update(output_dir, credentials_path, worksheet_name="SEN_party_coordinated"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    rows = build_rows()

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(GRAPHICS_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=max(len(rows) + 10, 20), cols=6)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    ws.format("A1:D1", {"textFormat": {"bold": True}})
    ws.format(f"D2:D{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows


if __name__ == "__main__":
    for row in build_rows():
        print(row)
