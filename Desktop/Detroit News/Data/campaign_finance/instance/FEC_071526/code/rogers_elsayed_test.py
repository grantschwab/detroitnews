"""
rogers_elsayed_test.py

Grant's experiment: a single tab ("RogersElSayed_test") combining
overallspend.py's cycle-to-date table and postprim_chart.py's
since-Aug-5-only table into one 4-row view, with a new "Period" column
distinguishing the two ("Since 2025 campaign launch" vs. "Post-primary")
instead of them living on two separate tabs.

Reuses overallspend.build_rows() and postprim_chart.build_rows()
directly rather than recomputing anything -- so this tab's numbers are
identical to SEN_overall_chart / SEN_postprim_chart, just re-shaped,
EXCEPT for one deliberate difference: UDP's Anti-Abdul spend is
subtracted out of both rows here (Grant's call -- he's adding his own
explanatory note about it directly in the sheet). SEN_overall_chart and
SEN_postprim_chart themselves are untouched -- this exclusion is local
to this tab only, same pattern as the UDP-specific column already added
to SEN_groups_chart_1M+ in groupspend.py.

Wired into outside_spending.py's 30-minute live-refresh loop as of
2026-09-23 (Grant confirmed he wants it live, not just a one-off) --
same _retry_on_quota-wrapped pattern every other graphics tab there
follows. Can still be run standalone for testing:

    export FEC_API_KEY="..."
    python3 rogers_elsayed_test.py
"""

import csv
import os

import overallspend
import postprim_chart

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

GRAPHICS_SHEET_ID = overallspend.GRAPHICS_SHEET_ID  # same "Michigan_SEN_spend" spreadsheet
OUTPUT_COLUMNS = ["Period", "Category", "El-Sayed campaign", "Rogers campaign", "Pro-Abdul",
                  "Anti-Rogers", "Pro-Rogers", "Anti-Abdul", "Total"]


UDP_RAW_NAME = "UNITED DEMOCRACY PROJECT ('UDP')"


def _udp_anti_abdul(output_dir):
    """UDP's Anti-Abdul spend, whole-cycle (SUM CandCategory) and
    since-Aug-5-only (Since Aug 5 Spent), read straight from
    outside_spending_2026.csv -- same raw-name match pattern already
    used in groupspend.py's UDP-specific column."""
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    whole_cycle, since_aug5 = 0.0, 0.0
    if not os.path.exists(path):
        return whole_cycle, since_aug5
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Outside Group", "").strip().upper() != UDP_RAW_NAME:
                continue
            if row.get("Candidate Name", "").strip().lower() != "elsayed":
                continue
            if row.get("Support/Oppose", "").strip() != "Oppose":
                continue
            try:
                whole_cycle += float(row.get("SUM CandCategory") or 0)
            except (TypeError, ValueError):
                pass
            try:
                since_aug5 += float(row.get("Since Aug 5 Spent") or 0)
            except (TypeError, ValueError):
                pass
    return whole_cycle, since_aug5


def _exclude_udp(row, udp_amount):
    """Anti-Abdul only lives on the "Rogers ..." row (build_rows()'s
    El-Sayed row always has Anti-Abdul=0.0) -- subtracting on every row
    would wrongly push El-Sayed's row negative."""
    row = dict(row)
    if row["Anti-Abdul"] > 0:
        row["Anti-Abdul"] -= udp_amount
        row["Total"] -= udp_amount
    return row


def build_rows(output_dir):
    udp_whole_cycle, udp_since_aug5 = _udp_anti_abdul(output_dir)

    rows = []
    for row in overallspend.build_rows(output_dir):
        rows.append({"Period": "Since 2025 campaign launch", **_exclude_udp(row, udp_whole_cycle)})
    for row in postprim_chart.build_rows(output_dir):
        rows.append({"Period": "Post-primary", **_exclude_udp(row, udp_since_aug5)})
    return rows


def update(output_dir, credentials_path, worksheet_name="RogersElSayed_test"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    rows = build_rows(output_dir)

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(GRAPHICS_SHEET_ID)
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=20, cols=10)

    def cell(r, c):
        v = r[c]
        if isinstance(v, (int, float)) and v == 0:
            return ""
        return v

    data = [OUTPUT_COLUMNS] + [[cell(r, c) for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    last_col = chr(64 + len(OUTPUT_COLUMNS))
    ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    ws.format(f"C2:{last_col}{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows


if __name__ == "__main__":
    rows = update("..", "../app-template-access-402821-3111eabfc82d.json")
    for row in rows:
        print(row)
