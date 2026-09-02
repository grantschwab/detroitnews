"""
postprim_chart.py

Live, continuously-updating since-Aug-5-2026-only version of
overallspend.py's "El-Sayed vs. Rogers" comparison table, pushed to the
"SEN_postprim_chart" tab in the same Flourish-facing graphics
spreadsheet. Same shape as overallspend_chart, but every figure is
windowed to spending on/after the post-primary cutoff instead of
cycle-to-date.

Outside money: outside_spending.py's compiled outside_spending_2026.csv
already carries a "Since Aug 5 Spent" column, computed fresh every
cycle -- read directly, no new pulls needed.

Campaign spend: NOT available from any already-compiled CSV -- Q2's
"C Expenditures"/"Q Expenditures" are cycle-to-date and quarter-scoped,
neither date-windowed to an arbitrary cutoff like Aug 5. A genuine
"campaign spend since Aug 5" figure requires itemized, dated disbursement
data, which only exists in each campaign's own filed reports' Schedule B
(Line 17, Operating Expenditures). This module downloads and parses the
most recently filed report for El-Sayed/Rogers directly via FastFEC
(same mechanism senate_ad_spend.py and outside_spending.py's RSS fast
path already use) -- confirmed 2026-08-11 that api.open.fec.gov's own
schedules/schedule_b/ endpoint has a real indexing gap for these
committees' recent months, so a live query there would silently
undercount.

Expected/correct, not a bug: as of 2026-08-19, neither candidate has
filed a report yet that covers any part of the Aug 5+ window (their last
filed report was the 12-day pre-primary report, through 2026-07-15; the
next, Q3, isn't due until mid-October) -- so campaign spend here will
show $0 for both until a report covering that window actually lands.
Re-checked every cycle (cheap: one filings-list API call per candidate),
so it starts populating on its own once a real report is filed, no code
change needed.

Called at the end of outside_spending.py's own sheet-upload step.
"""

import csv
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

BASE_URL = "https://api.open.fec.gov/v1"
FEC_DOCQUERY_TMPL = "https://docquery.fec.gov/dcdev/posted/{file_number}.fec"
FASTFEC = "fastfec"

CANDIDATES = {"elsayed": "C00902668", "rogers": "C00849810"}
POSTPRIM_CUTOFF = "2026-08-05"

GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"
OUTPUT_COLUMNS = ["Category", "El-Sayed campaign", "Rogers campaign", "Pro-Abdul",
                  "Anti-Rogers", "Pro-Rogers", "Anti-Abdul", "Total"]
COMMITTEE_TYPE_COLUMNS = OUTPUT_COLUMNS[1:-1]

# Separate from senate_ad_spend.py's .sb_filing_cache/ -- that one is a
# one-off historical cache, this is a live rolling one. Both may end up
# holding the same file_number's content, which is fine; kept apart to
# avoid conflating "one-off historical pull" with "live continuous pull"
# conceptually.
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".postprim_sb_cache")


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def query_fec(endpoint, params, retries=6):
    import time
    params = dict(params)
    params["api_key"] = os.environ.get("FEC_API_KEY", "")
    url = f"{BASE_URL}/{endpoint}?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
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


def _most_recent_filing(committee_id):
    """The single most-recently-filed F3 report for this committee, or
    None. Cheap -- one API call, no download."""
    data = query_fec(f"committee/{committee_id}/filings/", {"per_page": 5, "sort": "-coverage_end_date"})
    for r in data.get("results", []):
        if r.get("form_type") == "F3" and r.get("report_type"):
            return r
    return None


def _download_sb17(file_number):
    """Same pattern as senate_ad_spend.py's _download_sb17() -- downloads
    and parses one filing's Schedule B Line 17 rows via FastFEC, cached
    to disk by file_number."""
    cache_path = os.path.join(CACHE_DIR, f"{file_number}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    tmp_dir = os.path.join(CACHE_DIR, "_tmp", str(file_number))
    os.makedirs(tmp_dir, exist_ok=True)
    url = FEC_DOCQUERY_TMPL.format(file_number=file_number)
    cmd = f'curl -s "{url}" | {FASTFEC} -s {file_number} "{tmp_dir}/"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=90)
    sb_path = os.path.join(tmp_dir, str(file_number), "SB17.csv")
    if result.returncode != 0 or not os.path.exists(sb_path):
        return []

    rows = []
    with open(sb_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("memo_code") or "").strip().upper() == "X":
                continue
            rows.append({"date": row.get("expenditure_date"), "amount": row.get("expenditure_amount")})
    with open(cache_path, "w") as f:
        json.dump(rows, f)
    return rows


def _campaign_since_cutoff(committee_id):
    """0.0 if no filed report yet covers any part of the post-primary
    window (the expected state right now, not an error) -- otherwise
    sums that report's own itemized Schedule B disbursements dated on or
    after POSTPRIM_CUTOFF."""
    filing = _most_recent_filing(committee_id)
    if not filing or (filing.get("coverage_end_date") or "") < POSTPRIM_CUTOFF:
        return 0.0
    rows = _download_sb17(filing["file_number"])
    return sum(_to_float(r["amount"]) for r in rows if (r.get("date") or "") >= POSTPRIM_CUTOFF)


def _campaign_since_cutoff_all():
    os.makedirs(CACHE_DIR, exist_ok=True)
    return {slug: _campaign_since_cutoff(cid) for slug, cid in CANDIDATES.items()}


def _outside_since_cutoff(output_dir):
    """Same shape as overallspend.py's _outside_totals(), keyed off the
    Since Aug 5 Spent column instead of SUM CandCategory."""
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    totals = {
        ("elsayed", "Support"): 0.0, ("elsayed", "Oppose"): 0.0,
        ("rogers", "Support"): 0.0, ("rogers", "Oppose"): 0.0,
    }
    if not os.path.exists(path):
        return totals
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("Candidate Name", "").strip().lower(),
                   row.get("Support/Oppose", "").strip())
            if key in totals:
                totals[key] += _to_float(row.get("Since Aug 5 Spent"))
    return totals


def build_rows(output_dir):
    campaign = _campaign_since_cutoff_all()
    outside = _outside_since_cutoff(output_dir)

    values_by_category = {
        "El-Sayed and supporters": {
            "El-Sayed campaign": campaign["elsayed"],
            "Pro-Abdul": outside[("elsayed", "Support")],
            "Anti-Rogers": outside[("rogers", "Oppose")],
        },
        "Rogers and supporters": {
            "Rogers campaign": campaign["rogers"],
            "Pro-Rogers": outside[("rogers", "Support")],
            "Anti-Abdul": outside[("elsayed", "Oppose")],
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


def update_postprim_chart(output_dir, credentials_path, worksheet_name="SEN_postprim_chart"):
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

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    last_col = chr(64 + len(OUTPUT_COLUMNS))
    ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    ws.format(f"B2:{last_col}{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows
