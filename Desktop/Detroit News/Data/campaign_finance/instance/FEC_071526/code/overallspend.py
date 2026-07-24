"""
overallspend.py

Builds the Stevens vs. El-Sayed spend-comparison table (campaign spend +
outside-group spend by support/oppose direction) and pushes it to the
"overallspend_chart" Google Sheet tab, feeding a live-updating Flourish
graphic.

Reads only from the already-compiled output CSVs written by
monitor_preprimary.py (campaign_finance_2026_preprimary.csv -- 12-day
pre-primary filings, more current than the Q2 quarterly filing this
replaced) and outside_spending.py (outside_spending_2026.csv) -- no new
API calls. Called at the end of each of those two scripts' own
sheet-upload step so this tab reflects whatever either pipeline most
recently pulled.
"""

import csv
import os

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

OUTPUT_COLUMNS = ["Category", "Stevens campaign", "El-Sayed campaign", "Pro-Haley",
                  "Anti-Abdul", "Pro-Abdul", "Anti-Haley", "Total"]

# Graphics tabs live in a separate spreadsheet from the main tracking
# sheet (candidate filings, raw outside-spending data) -- Flourish-facing
# only. Ignores whatever --sheet-id the caller passes for its own tab.
GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"

COMMITTEE_TYPE_COLUMNS = OUTPUT_COLUMNS[1:-1]


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _campaign_expenditures(output_dir):
    """Cycle-to-date operating expenditures ('C Expenditures') for stevens/elsayed,
    from their 12-day pre-primary (12P) filings -- more current than the Q2
    quarterly filing this replaced."""
    path = os.path.join(output_dir, "output", "campaign_finance_2026_preprimary.csv")
    result = {"stevens": 0.0, "elsayed": 0.0}
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("Candidate Name", "").strip().lower()
            if name in result:
                result[name] = _to_float(row.get("C Expenditures"))
    return result


def _outside_totals(output_dir):
    """Sum of SUM CandCategory (per-group cycle total) across every group,
    grouped by (candidate, support/oppose)."""
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    totals = {
        ("stevens", "Support"): 0.0, ("stevens", "Oppose"): 0.0,
        ("elsayed", "Support"): 0.0, ("elsayed", "Oppose"): 0.0,
    }
    if not os.path.exists(path):
        return totals
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("Candidate Name", "").strip().lower(),
                   row.get("Support/Oppose", "").strip())
            if key in totals:
                totals[key] += _to_float(row.get("SUM CandCategory"))
    return totals


def build_rows(output_dir):
    campaign = _campaign_expenditures(output_dir)
    outside = _outside_totals(output_dir)

    values_by_category = {
        "Stevens and supporters": {
            "Stevens campaign": campaign["stevens"],
            "Pro-Haley": outside[("stevens", "Support")],
            "Anti-Abdul": outside[("elsayed", "Oppose")],
        },
        "El-Sayed and supporters": {
            "El-Sayed campaign": campaign["elsayed"],
            "Pro-Abdul": outside[("elsayed", "Support")],
            "Anti-Haley": outside[("stevens", "Oppose")],
        },
    }

    rows = []
    for category, values in values_by_category.items():
        row = {"Category": category}
        for column in COMMITTEE_TYPE_COLUMNS:
            row[column] = values.get(column, 0.0)
        row["Total"] = sum(row[c] for c in COMMITTEE_TYPE_COLUMNS)
        rows.append(row)
    return rows


def update_overallspend_chart(output_dir, sheet_id, credentials_path, worksheet_name="SEN_overall_chart"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    sheet_id = GRAPHICS_SHEET_ID  # graphics tabs always target the dedicated Flourish sheet
    rows = build_rows(output_dir)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=20, cols=10)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    last_col = chr(64 + len(OUTPUT_COLUMNS))
    ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    ws.format(f"B2:{last_col}{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows
