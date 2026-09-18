"""
race_totals.py

Whole-race spending totals for the MI Senate seat, comparing the current
2026 cycle (El-Sayed vs. Rogers) against the last time this seat's race
happened this expensive, 2024 (Rogers vs. Slotkin). Two rows -- one per
cycle -- each summing ALL real primary candidates on both sides (not
just the two general-election nominees) plus ALL outside-group
independent expenditure spending on any candidate in the race, whether
or not they won their primary.

Built from the same one-off investigation Grant asked for in chat
2026-09-18 ("how much money has been spent on the race, including
candidates who didn't win their primary, vs. 2024"), now turned into a
permanent, live-updating tab ("SEN_race_totals") instead of a one-time
answer.

CANDIDATE LISTS: hardcoded below, not auto-discovered every cycle --
determining "who actually ran" required real judgment calls (see notes
on each dict) that shouldn't silently change if the FEC search API's
results shift. Confirmed via candidates/search/?state=MI&office=S&
election_year=YYYY against candidate_status: only status="C" ("covered"
-- an active statutory candidate) counted, and even then two incumbents
who retired rather than ran (Gary Peters in 2026, Debbie Stabenow in
2024) were manually excluded despite technically appearing with
status="C" -- their committees are just winding down, they were never
actual candidates in the race being measured here.

"Candidate" total = disbursements (money actually SPENT, not raised) via
each committee's own committee/{id}/totals/ endpoint, cycle-to-date.
"Outside group" total = every outside group's Schedule E independent
expenditures against ANY candidate in CANDIDATES_2026/2024 below (not
just the two general-election nominees) -- reuses
outside_spending.fetch_schedule_e_for_contest() +
dedupe_notice_vs_periodic() (the same proven notice-vs-periodic dedup
logic the live pipeline itself depends on), then filters results to
only the last names present in that cycle's candidate dict -- this is
what excludes a handful of stray/irrelevant Schedule E rows that
nominally mention Slotkin or Peters in the 2026 query (noise from the
broad state+office search, not real 2026-race spending; confirmed
negligible, ~$38k total, when this was first investigated).

Called at the end of outside_spending.py's own sheet-upload step.
"""

import os

import outside_spending as osp

CANDIDATES_2026 = {
    # DEM primary
    "elsayed": "C00902668",     # El-Sayed -- won primary, nominee
    "howard": "C00906867",      # Rachel Howard
    "mcmorrow": "C00901173",    # Mallory McMorrow
    "stevens": "C00903039",     # Haley Stevens
    "tate": "C00904862",        # Joseph Allen Tate
    # REP primary
    "heurtebise": "C00897736",  # Frederick Heurtebise
    "rogers": "C00849810",      # Rogers -- won primary, nominee
    "scott": "C00903427",       # Genevieve Scott
    "smith": "C00926279",       # Bernadette Smith
    # Gary Peters (incumbent, retiring, never actually ran) deliberately
    # excluded -- see module docstring.
}

CANDIDATES_2024 = {
    # DEM primary
    "slotkin": "C00834218",  # Slotkin -- won primary, won general
    "harper": "C00844985",   # Frank Eugene Hill Harper
    "beydoun": "C00837112",  # Nasser Beydoun
    "burns": "C00837856",    # Zack Burns
    "love": "C00839183",     # Leslie N. Love
    # REP primary
    "rogers": "C00849810",    # Rogers -- won primary, lost general
    "pensler": "C00858761",   # Sandy Pensler
    "amash": "C00476291",     # Justin Amash
    "meijer": "C00855668",    # Peter Meijer
    "odonnell": "C00847657",  # Sherrell Anne O'Donnell
    "wilson": "C00865063",    # Glenn Allen Wilson
    "hoover": "C00832618",    # Michael Hoover
    "craig": "C00851964",     # James Craig
    "savage": "C00848879",    # Sharon Maureen Savage
    "taylor": "C00842716",    # Alexandria J. Taylor
    "curran": "C00871335",    # Rebekah A. Curran
    # jdwilson deliberately keyed differently from "wilson" (Glenn Allen
    # Wilson) above -- two different Wilsons ran.
    "jdwilson": "C00848986",  # J.D. Wilson
    "solismullen": "C00884742",  # Joseph Solis-Mullen
    # Debbie Stabenow (incumbent, retiring, never actually ran)
    # deliberately excluded -- see module docstring.
}

GRAPHICS_SHEET_ID = osp.__dict__.get("GRAPHICS_SHEET_ID") or "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"
OUTPUT_COLUMNS = ["Year", "Candidates", "Outside groups", "Total"]

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _candidate_spend(committee_ids, cycle):
    total = 0.0
    for committee_id in set(committee_ids):
        data = osp.query_fec(f"committee/{committee_id}/totals/", {"cycle": cycle, "per_page": 5})
        results = data.get("results", [])
        if results:
            total += _to_float(results[0].get("disbursements"))
    return total


def _alnum(s):
    return "".join(c for c in s.upper() if c.isalnum())


def _outside_spend(candidates, cycle):
    slugs = {_alnum(slug) for slug in candidates}
    txns = osp.fetch_schedule_e_for_contest("MI", "S", None, cycle, f"{cycle - 1}-01-01")
    txns = osp.dedupe_notice_vs_periodic(txns)
    total = 0.0
    for t in txns:
        # Matched alnum-only against the candidates dict's slug keys
        # (already alnum-only) -- avoids a real candidate getting
        # dropped over a stray hyphen/apostrophe/space formatting
        # difference between the two data sources (e.g. "O'DONNELL" vs.
        # slug "odonnell", "EL-SAYED" vs. "elsayed").
        if _alnum(t.get("candidate_last_name") or "") in slugs:
            total += _to_float(t.get("expenditure_amount"))
    return total


YEAR_LABELS = {2024: "2024", 2026: "2026 (so far)"}


def build_rows():
    rows = []
    for year, candidates in ((2024, CANDIDATES_2024), (2026, CANDIDATES_2026)):
        print(f"{year}:")
        candidate_total = _candidate_spend(candidates.values(), year)
        print(f"  candidate disbursements: ${candidate_total:,.0f}")
        outside_total = _outside_spend(candidates, year)
        print(f"  outside group spend: ${outside_total:,.0f}")
        rows.append({
            "Year": YEAR_LABELS[year],
            "Candidates": round(candidate_total, 2),
            "Outside groups": round(outside_total, 2),
            "Total": round(candidate_total + outside_total, 2),
        })
    return rows


def update(output_dir, credentials_path, worksheet_name="SEN_race_totals"):
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
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=10, cols=6)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    ws.format("A1:D1", {"textFormat": {"bold": True}})
    ws.format(f"B2:D{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows


if __name__ == "__main__":
    rows = build_rows()
    for row in rows:
        print(row)
