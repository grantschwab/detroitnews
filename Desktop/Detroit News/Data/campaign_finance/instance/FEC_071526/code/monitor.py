"""
monitor.py

Polls the FEC API every 15 minutes for new filings from candidates in
candidates.csv. When a new or amended filing is detected, it downloads
and parses it with FastFEC, recompiles the summary CSV, and sends an
email notification.

Usage:
    # Run from the code/ directory:
    python monitor.py \\
        --candidates candidates.csv \\
        --output-dir '..' \\
        --cycle 2026 \\
        --quarter Q1

    # For a test run (Q4 2025, shorter poll interval):
    python monitor.py \\
        --candidates candidates.csv \\
        --output-dir '..' \\
        --cycle 2025 \\
        --quarter Q4 \\
        --poll-interval 60

Required environment variables:
    FEC_API_KEY       FEC API key
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

import overallspend
import groupspend

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = "https://api.open.fec.gov/v1"
FEC_DOCQUERY_TMPL = "https://docquery.fec.gov/dcdev/posted/{file_number}.fec"

# Coverage end dates per quarter (month-day)
QUARTER_END = {
    "Q1": "03-31",
    "Q2": "06-30",
    "Q3": "09-30",
    "Q4": "12-31",
}

# F3N column → output column (mirrors original notebook)
COLUMNS_MAPPING = {
    "committee_name":                        "Candidate Committee",
    "col_a_total_contributions":             "Q Total_contributions",
    "col_a_total_receipts":                  "Q Total_Receipts",
    "col_a_transfers_from_authorized":       "Q Transfer_auth_committees",
    "col_a_individual_contributions_itemized": "Q Individual_Itemized",
    "col_a_cash_on_hand_close_of_period":    "Q Cash on Hand",
    "col_a_candidate_loans":                 "Q Self Loans",
    "col_a_total_operating_expenditures":    "Q Expenditures",
    "col_a_debts_by":                        "Q Debt",
    "col_b_total_receipts":                  "C Total_Receipts",
    "col_b_individual_contributions_itemized": "C Individual_Itemized",
    "col_b_total_operating_expenditures":    "C Expenditures",
    "col_b_candidate_loans":                 "C Self Loans",
}

OUTPUT_COLUMNS = [
    "Candidate Name",
    "First Name",
    "District",
    "Party",
    "Contest ID",
    "Q Receipts minus Loans",
    "Q Total_contributions",
    "Q Total_Receipts",
    "Q Transfer_auth_committees",
    "Q Individual_Itemized",
    "Q Cash on Hand",
    "Q Self Loans",
    "Q Expenditures",
    "Q Debt",
    "Q Burn Rate",
    "C Total_Receipts",
    "C Individual_Itemized",
    "C Expenditures",
    "C Self Loans",
    "Candidate Committee",
    "FEC Link",
    "Amendment",
]


# ── FEC API ───────────────────────────────────────────────────────────────────

def query_fec(endpoint, params, api_key, retries=3):
    params = dict(params)
    params["api_key"] = api_key
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(5)
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                raise
    return {}


def get_latest_filing(committee_id, cycle, quarter, api_key):
    """
    Returns the most recently filed House/Senate report for the given
    committee and quarter. Fetches recent reports sorted by coverage_end_date
    and filters client-side, then picks the highest file_number (latest amendment).
    Returns None if no filing found.
    """
    end_date = f"{cycle}-{QUARTER_END[quarter]}"

    params = {
        "committee_id": committee_id,
        "sort": "-coverage_end_date",
        "per_page": 10,
    }
    data = query_fec("reports/house-senate/", params, api_key)
    results = data.get("results", [])
    matches = [r for r in results if (r.get("coverage_end_date") or "").startswith(end_date)]
    if not matches:
        return None
    # Highest file_number = most recently filed (original or latest amendment)
    return max(matches, key=lambda r: r.get("file_number", 0))


# ── FEC Electronic Filing RSS feed ──────────────────────────────────────────
#
# api.open.fec.gov (used above) indexes filings hours after they post --
# this is the same real-time feed FEC Notify emails are built on, filterable
# directly by our own committee IDs. Used to auto-seed force_id without
# waiting on the API or a human pasting a filing number from an email.

RSS_BASE_URL = "https://efilingapps.fec.gov/rss/generate"
RSS_ITEM_RE = re.compile(
    r"CommitteeId:\s*(?P<committee_id>[A-Z0-9]+)\s*\|\s*FilingId:\s*(?P<file_number>\d+)"
    r"\s*\|\s*FormType:\s*(?P<form_type>[A-Z0-9]+)"
    r"\s*\|\s*CoverageFrom:\s*(?P<coverage_from>[\d/]*)"
    r"\s*\|\s*CoverageThrough:\s*(?P<coverage_through>[\d/]*)"
)
# Periodic candidate/committee reports only -- excludes statements of
# organization (F1), 24/48-hour IE notices (F24/F5), etc. Covers both new
# ("F3N"/"F3X") and amended ("F3A", "F3XA") periodic reports -- confirmed
# missing "F3A" in production on 2026-07-15 (Ufford's amendment sat
# undetected because the old pattern ^F3[NX] doesn't match bare "F3A").
RELEVANT_FORM_TYPES = re.compile(r"^F3[NXA]")


def _mmddyyyy_to_iso(s):
    """'09/30/2025' -> '2025-09-30'. Returns None if not parseable."""
    parts = (s or "").split("/")
    if len(parts) != 3:
        return None
    mm, dd, yyyy = parts
    return f"{yyyy}-{mm}-{dd}"


def fetch_rss_force_ids(committee_ids, cycle, quarter):
    """
    Queries the FEC Electronic Filing RSS feed for the given committee IDs
    (last 7 days of filings) and returns {committee_id: (file_number,
    is_amendment)} using the most recent filing per committee whose
    CoverageThrough actually matches the target quarter.

    A committee can amend an OLDER report (e.g. last quarter's) at any
    time, and the RSS feed doesn't separate "new filing" from "amendment
    of something old" -- confirmed in production on 2026-07-15, where
    Stevens amending her Q3 2025 report got force-loaded as if it were
    her Q2 2026 filing, because the old version of this function just
    took the single most recent F3-family filing regardless of period.
    Coverage dates must be checked, same as get_latest_filing() already
    does for the API path.

    is_amendment is derived from the form type (e.g. "F3A" vs "F3N") so
    callers can label an RSS-sourced filing correctly instead of assuming
    it's always an original. Returns {} on any error -- this is a
    nice-to-have optimization, never a hard dependency.
    """
    if not committee_ids:
        return {}
    try:
        url = f"{RSS_BASE_URL}?{urllib.parse.urlencode({'cids': ','.join(committee_ids)})}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  RSS feed fetch failed (non-fatal): {e}")
        return {}

    target_end = f"{cycle}-{QUARTER_END[quarter]}"
    force_ids = {}
    # Feed lists newest-first; keep only the first (most recent) match per committee.
    for m in RSS_ITEM_RE.finditer(body):
        cid = m.group("committee_id")
        if cid in force_ids:
            continue
        form_type = m.group("form_type")
        if not RELEVANT_FORM_TYPES.match(form_type):
            continue
        coverage_through = _mmddyyyy_to_iso(m.group("coverage_through"))
        if coverage_through != target_end:
            continue
        is_amendment = "A" in form_type[2:]  # e.g. "F3A", "F3XA" vs "F3N", "F3X"
        force_ids[cid] = (int(m.group("file_number")), is_amendment)
    return force_ids


# ── FastFEC ───────────────────────────────────────────────────────────────────

def run_fastfec(file_number, candidate_dir):
    """
    Streams the .fec filing through FastFEC and writes parsed CSVs
    into candidate_dir/{file_number}/. Overwrites any existing files.
    Structure: raw/{contest_id}/{party}/{candidate_name}/{file_number}/F3N.csv
    """
    os.makedirs(candidate_dir, exist_ok=True)
    url = FEC_DOCQUERY_TMPL.format(file_number=file_number)
    cmd = f'curl -s "{url}" | fastfec -s {file_number} "{candidate_dir}/"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FastFEC exited {result.returncode}: {result.stderr.strip()}")


def verify_filing_coverage(candidate_dir, file_number, cycle, quarter):
    """
    Reads coverage_through_date directly from the just-downloaded F3N/F3A
    CSV and confirms it actually matches the target quarter. This is the
    final, ground-truth gate -- it doesn't matter whether the file_number
    came from the API, the RSS feed, a manually-pasted force_id, or a
    --force flag; if a committee amends an OLDER report, that filing can
    otherwise slip through and get mislabeled as the current quarter.

    Confirmed happening in production on 2026-07-15: three candidates
    (Stevens, Ufford, McCann) had an old-quarter amendment briefly loaded
    as their Q2 2026 report. The RSS coverage-date check added the same
    day closes the RSS path specifically, but this check is the backstop
    for every path, present and future.

    Returns True if the coverage matches, False otherwise (and does NOT
    raise -- a missing/malformed file here should be treated as "couldn't
    verify," not a crash).
    """
    target_end = f"{cycle}-{QUARTER_END[quarter]}"
    folder = os.path.join(candidate_dir, str(file_number))
    for name in ("F3N.csv", "F3A.csv"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    row = next(csv.DictReader(f), None)
                return bool(row) and row.get("coverage_through_date") == target_end
            except Exception:
                return False
    return False


# ── CSV compilation ───────────────────────────────────────────────────────────

def compile_csv(raw_dir, output_path, state):
    """
    Reads all F3N.csv files from candidate subdirectories under raw_dir
    and compiles them into a single summary CSV at output_path.
    Returns the number of rows written.
    """
    # Build a reverse lookup: (candidate_name, contest_id) -> committee state
    meta_lookup = {}
    for cid, info in state.items():
        key = (info.get("candidate_name", ""), info.get("contest_id", ""))
        meta_lookup[key] = info

    rows = []
    # Walk raw/{contest_id}/{party}/{candidate_name}/
    for contest_id in sorted(os.listdir(raw_dir)):
        contest_path = os.path.join(raw_dir, contest_id)
        if not os.path.isdir(contest_path):
            continue
        for party in sorted(os.listdir(contest_path)):
            party_path = os.path.join(contest_path, party)
            if not os.path.isdir(party_path):
                continue
            for candidate_name in sorted(os.listdir(party_path)):
                folder_path = os.path.join(party_path, candidate_name)
                if not os.path.isdir(folder_path):
                    continue

                # FastFEC names the summary CSV after the actual form type
                # submitted: "F3N.csv" for an original filing, "F3A.csv" for
                # an amendment -- same schema either way (confirmed 2026-07-15,
                # both have identical headers), so check both. Missed Ufford's
                # amendment in production because this only checked F3N.
                # Use highest-numbered subdir = latest amendment.
                candidate_summary_names = ("F3N.csv", "F3A.csv")
                f3n_path = None
                for name in candidate_summary_names:
                    candidate = os.path.join(folder_path, name)
                    if os.path.isfile(candidate):
                        f3n_path = candidate
                        break
                if f3n_path is None:
                    subdirs = sorted(
                        [d for d in os.listdir(folder_path) if os.path.isdir(os.path.join(folder_path, d))],
                        key=lambda x: int(x) if x.isdigit() else 0,
                        reverse=True,
                    )
                    for subdir in subdirs:
                        for name in candidate_summary_names:
                            candidate = os.path.join(folder_path, subdir, name)
                            if os.path.isfile(candidate):
                                f3n_path = candidate
                                break
                        if f3n_path is not None:
                            break
                if f3n_path is None:
                    continue

                try:
                    with open(f3n_path, newline="", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        for raw_row in reader:
                            row = {}
                            for src, dst in COLUMNS_MAPPING.items():
                                val = raw_row.get(src, "")
                                # Convert dollar fields to rounded floats
                                if dst not in ("Candidate Committee", "District"):
                                    try:
                                        row[dst] = round(float(val), 2) if val.strip() else ""
                                    except (ValueError, AttributeError):
                                        row[dst] = val
                                else:
                                    row[dst] = val

                            # Calculated fields
                            try:
                                receipts = float(row.get("Q Total_Receipts") or 0)
                                loans    = float(row.get("Q Self Loans") or 0)
                                row["Q Receipts minus Loans"] = round(receipts - loans, 2)
                            except (ValueError, TypeError):
                                row["Q Receipts minus Loans"] = ""

                            try:
                                expenditures = float(row.get("Q Expenditures") or 0)
                                receipts     = float(row.get("Q Total_Receipts") or 0)
                                row["Q Burn Rate"] = round(expenditures / receipts, 4) if receipts else ""
                            except (ValueError, TypeError):
                                row["Q Burn Rate"] = ""

                            row["Contest ID"]     = contest_id
                            row["Candidate Name"] = candidate_name
                            # Derive district from contest_id (e.g. "mi04" → "04")
                            # rather than from the filing's election_district field,
                            # which can reflect an old or incorrect district.
                            row["District"] = contest_id[2:] if len(contest_id) > 2 else ""

                            # Amendment label, party, and FEC link
                            meta = meta_lookup.get((candidate_name, contest_id), {})
                            indicator = meta.get("amendment_indicator", "N")
                            row["Amendment"] = "Amendment" if indicator == "A" else "Original"
                            row["Party"] = meta.get("party", "")
                            row["First Name"] = meta.get("first_name", "").lower()
                            cid = meta.get("committee_id", "")
                            row["FEC Link"] = f"https://www.fec.gov/data/committee/{cid}/" if cid else ""

                            rows.append(row)

                except Exception as e:
                    print(f"    Warning: could not read {f3n_path}: {e}")

    if not rows:
        return 0

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


# ── Google Sheets ────────────────────────────────────────────────────────────

# Columns to highlight in light yellow
HIGHLIGHT_COLUMNS = {"Q Total_Receipts", "Q Cash on Hand", "Q Expenditures", "C Total_Receipts"}
LIGHT_YELLOW = {"red": 1.0, "green": 0.98, "blue": 0.7}

# Columns to format as dollars (whole number with commas)
DOLLAR_COLUMNS = {
    "Q Receipts minus Loans", "Q Total_contributions", "Q Total_Receipts",
    "Q Transfer_auth_committees", "Q Individual_Itemized", "Q Cash on Hand",
    "Q Self Loans", "Q Expenditures", "Q Debt",
    "C Total_Receipts", "C Individual_Itemized", "C Expenditures", "C Self Loans",
}


def col_letter(n):
    """Convert 0-based column index to Sheets letter (0→A, 25→Z, 26→AA)."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell_format(col_name):
    """Return the userEnteredFormat dict for a given column name."""
    fmt = {}
    if col_name in HIGHLIGHT_COLUMNS:
        fmt["backgroundColor"] = LIGHT_YELLOW
    if col_name in DOLLAR_COLUMNS:
        fmt["numberFormat"] = {"type": "NUMBER", "pattern": "#,##0"}
    if col_name == "Q Burn Rate":
        fmt["numberFormat"] = {"type": "PERCENT", "pattern": "0.0%"}
    return fmt


def upload_to_sheets(csv_path, sheet_id, credentials_path, worksheet_name=None):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(sheet_id)
    if worksheet_name:
        try:
            ws = spreadsheet.worksheet(worksheet_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=worksheet_name, rows=200, cols=50)
    else:
        ws = spreadsheet.sheet1

    with open(csv_path, newline="", encoding="utf-8") as f:
        data = list(csv.reader(f))

    # Convert numeric columns to Python floats
    numeric_cols = DOLLAR_COLUMNS | {"Q Burn Rate"}
    numeric_indices = {i for i, col in enumerate(OUTPUT_COLUMNS) if col in numeric_cols}
    converted = []
    for row_idx, row in enumerate(data):
        if row_idx == 0:
            converted.append(row)
            continue
        new_row = []
        for col_idx, val in enumerate(row):
            if col_idx in numeric_indices and val not in ("", None):
                try:
                    new_row.append(float(str(val).replace(",", "")))
                except ValueError:
                    new_row.append(val)
            else:
                new_row.append(val)
        converted.append(new_row)
    data = converted

    # Pad with 20 extra columns and 100 blank rows
    if data:
        data = [row + [""] * 20 for row in data]
        empty_row = [""] * len(data[0])
        data += [empty_row[:] for _ in range(100)]

    # Build rows with value + format set atomically for every cell.
    # This eliminates any race between separate write and format calls.
    col_fmts = [_cell_format(col) for col in OUTPUT_COLUMNS]
    rows = []
    for row_idx, row in enumerate(data):
        cells = []
        for col_idx, val in enumerate(row):
            cell = {}
            # Value
            if isinstance(val, float):
                cell["userEnteredValue"] = {"numberValue": val}
            elif isinstance(val, int):
                cell["userEnteredValue"] = {"numberValue": float(val)}
            elif val == "" or val is None:
                cell["userEnteredValue"] = {}
            else:
                cell["userEnteredValue"] = {"stringValue": str(val)}
            # Format (skip header row and extra padding columns)
            if row_idx > 0 and col_idx < len(OUTPUT_COLUMNS):
                fmt = col_fmts[col_idx]
                if fmt:
                    cell["userEnteredFormat"] = fmt
            cells.append(cell)
        rows.append({"values": cells})

    spreadsheet.batch_update({"requests": [
        # Write all values + formatting in one atomic call
        {
            "updateCells": {
                "rows": rows,
                "fields": "userEnteredValue,userEnteredFormat.numberFormat,userEnteredFormat.backgroundColor",
                "start": {"sheetId": ws.id, "rowIndex": 0, "columnIndex": 0},
            }
        },
        # Filter with Q Total_Receipts > 5000
        {
            "setBasicFilter": {
                "filter": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0,
                        "startColumnIndex": 0,
                        "endColumnIndex": len(OUTPUT_COLUMNS),
                    },
                    "filterSpecs": [{
                        "columnIndex": OUTPUT_COLUMNS.index("Q Total_Receipts"),
                        "filterCriteria": {
                            "condition": {
                                "type": "NUMBER_GREATER",
                                "values": [{"userEnteredValue": "5000"}]
                            }
                        }
                    }]
                }
            }
        },
    ]})


# ── Notification ─────────────────────────────────────────────────────────────

def notify(candidate_name, committee_name, amendment_indicator):
    surname = candidate_name.title()
    filing_type = "Amendment" if amendment_indicator == "A" else "New filing"
    message = f"{filing_type}: {committee_name}"
    subprocess.run([
        "osascript", "-e",
        f'display notification "{message}" with title "FEC Update: {surname}"'
    ])


# ── State helpers ─────────────────────────────────────────────────────────────

def load_state(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monitor FEC for new candidate filings.")
    parser.add_argument("--candidates",      default="../code/candidates.csv",
                        help="Path to clean candidates CSV (from preprocess.py)")
    parser.add_argument("--output-dir",      required=True,
                        help="Base output directory for FastFEC files and compiled CSV")
    parser.add_argument("--cycle",           required=True, type=int,
                        help="Election cycle year (e.g. 2026)")
    parser.add_argument("--quarter",         required=True, choices=["Q1", "Q2", "Q3", "Q4"],
                        help="Reporting quarter")

    parser.add_argument("--poll-interval",   type=int, default=900,
                        help="Seconds between polls (default: 900 = 15 min)")
    parser.add_argument("--stop-when-complete", action="store_true",
                        help="Exit automatically once every candidate has filed at least once")
    parser.add_argument("--once", action="store_true",
                        help="Run a single pass and exit (no polling loop)")
    parser.add_argument("--sheet-id",    default="10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc",
                        help="Google Sheets spreadsheet ID")
    parser.add_argument("--worksheet",   default=None,
                        help="Worksheet/tab name to write to (created if missing). "
                             "Defaults to the spreadsheet's first sheet.")
    parser.add_argument("--credentials", default=os.path.join(
                            os.path.dirname(os.path.abspath(__file__)), "..",
                            "app-template-access-402821-3111eabfc82d.json"),
                        help="Path to Google service account credentials JSON")
    parser.add_argument("--force", action="append", metavar="COMMITTEE_ID:FILE_NUMBER",
                        help="Force-process a specific filing (bypasses API). "
                             "Can be passed multiple times. "
                             "Example: --force C00711317:1962265")
    parser.add_argument("--no-rss", action="store_true",
                        help="Disable auto-detection via the FEC Electronic Filing "
                             "RSS feed (falls back to manual force_id / API only)")
    parser.add_argument("--raw-subdir", default="raw",
                        help="Subdirectory name under --output-dir for downloaded "
                             "filing data (default: raw). Use a distinct value when "
                             "running a second instance against a different quarter "
                             "against the same --output-dir, so file_numbers from "
                             "different quarters don't collide in one candidate folder.")
    parser.add_argument("--state-file", default=".monitor_state.json",
                        help="Filename (under --output-dir) for the persisted "
                             "state JSON (default: .monitor_state.json). Use a "
                             "distinct value alongside --raw-subdir when running "
                             "a second instance against a different quarter.")
    args = parser.parse_args()

    api_key = os.environ.get("FEC_API_KEY", "")

    if not api_key:
        sys.exit("Error: FEC_API_KEY environment variable not set.")

    # Paths
    raw_dir     = os.path.join(args.output_dir, args.raw_subdir)
    output_csv  = os.path.join(args.output_dir, "output",
                               f"campaign_finance_{args.cycle}_{args.quarter}.csv")
    state_path  = os.path.join(args.output_dir, args.state_file)

    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(os.path.join(args.output_dir, "output"), exist_ok=True)

    # Load candidates
    with open(args.candidates, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    print(f"Loaded {len(candidates)} candidates.")
    print(f"Monitoring {args.cycle} {args.quarter} | output: {args.output_dir}")
    print(f"Poll interval: {args.poll_interval}s | stop when complete: {args.stop_when_complete}\n")

    # Load persisted state and seed candidate metadata
    state = load_state(state_path)
    for cand in candidates:
        cid = cand["committee_id"]
        if cid not in state:
            state[cid] = {}
        # Always refresh metadata from candidates.csv in case it changed
        state[cid]["candidate_name"] = cand["candidate_name"]
        state[cid]["contest_id"]     = cand["contest_id"]
        state[cid]["committee_name"] = cand["committee_name"]
        state[cid]["party"]          = cand.get("party", "OTH")
    save_state(state_path, state)

    # Main polling loop
    try:
        while True:
            # Reload candidates.csv and force_map each cycle so edits made
            # while the script is running are picked up without a restart.
            with open(args.candidates, newline="", encoding="utf-8") as f:
                candidates = list(csv.DictReader(f))
            for cand in candidates:
                cid = cand["committee_id"]
                if cid not in state:
                    state[cid] = {}
                state[cid]["candidate_name"] = cand["candidate_name"]
                state[cid]["contest_id"]     = cand["contest_id"]
                state[cid]["committee_name"] = cand["committee_name"]
                state[cid]["party"]          = cand.get("party", "OTH")
                state[cid]["committee_id"]   = cid
                state[cid]["first_name"]     = cand.get("first_name", "")

            # Lowest priority: RSS feed auto-detection (beats the API's own
            # lag without needing a human to paste a filing number from a
            # FEC Notify email). Manual force_id / --force below override it.
            force_map = {}
            if not args.no_rss:
                force_map.update(fetch_rss_force_ids(
                    [c["committee_id"] for c in candidates], args.cycle, args.quarter))

            for cand in candidates:
                raw_fid = cand.get("force_id", "").strip()
                if raw_fid:
                    try:
                        # Amendment status unknown for a manually-pasted filing
                        # number -- default to False (best guess: "Original").
                        force_map[cand["committee_id"]] = (int(raw_fid), False)
                    except ValueError:
                        pass
            # CLI --force flags are merged in each cycle too
            if args.force:
                for entry in args.force:
                    if ":" in entry:
                        cid, fnum = entry.split(":", 1)
                        try:
                            force_map[cid] = (int(fnum), False)
                        except ValueError:
                            pass

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            filed_count   = sum(1 for c in candidates if state.get(c["committee_id"], {}).get("file_number"))
            pending_count = len(candidates) - filed_count
            print(f"[{now}] {filed_count}/{len(candidates)} filed — checking {pending_count} pending...")

            newly_processed = []

            for cand in candidates:
                cid = cand["committee_id"]

                try:
                    filing = get_latest_filing(cid, args.cycle, args.quarter, api_key)
                except Exception as e:
                    print(f"  {cand['candidate_name']}: API error — {e}")
                    time.sleep(0.5)
                    continue

                time.sleep(0.2)  # gentle rate limiting

                if not filing:
                    # Fall back to force_id if API hasn't indexed the filing yet
                    forced = force_map.get(cid)
                    if forced:
                        file_number, is_amendment = forced
                        amendment_indicator = "A" if is_amendment else "N"
                        print(f"  {cand['candidate_name']:<20} no API filing — using force_id #{file_number}")
                    else:
                        print(f"  {cand['candidate_name']:<20} no filing yet")
                        continue
                else:
                    file_number         = filing.get("file_number")
                    amendment_indicator = filing.get("amendment_indicator", "N")
                last_file_number     = state[cid].get("file_number")

                if file_number == last_file_number:
                    label = "Amendment" if amendment_indicator == "A" else "Original"
                    print(f"  {cand['candidate_name']:<20} already current (#{file_number}, {label})")
                    continue

                # New or amended filing detected
                is_update = last_file_number is not None
                action    = "AMENDMENT" if is_update else "NEW FILING"
                print(f"  {cand['candidate_name']:<20} {action} #{file_number} — downloading...")

                party = state[cid].get("party", "OTH")
                candidate_dir = os.path.join(raw_dir, cand["contest_id"], party, cand["candidate_name"])
                try:
                    run_fastfec(file_number, candidate_dir)
                except Exception as e:
                    print(f"    FastFEC error: {e}")
                    continue

                # Ground-truth check: does the filing we just downloaded
                # actually cover this quarter, regardless of where the
                # file_number came from? Reject and skip if not -- do NOT
                # touch state, so it stays correctly "no filing yet".
                if not verify_filing_coverage(candidate_dir, file_number, args.cycle, args.quarter):
                    print(f"    REJECTED: #{file_number} does not cover {args.quarter} {args.cycle} "
                          f"(likely an amendment to an older report) -- discarding.")
                    shutil.rmtree(os.path.join(candidate_dir, str(file_number)), ignore_errors=True)
                    continue

                # Persist state immediately
                state[cid]["file_number"]            = file_number
                state[cid]["amendment_indicator"] = amendment_indicator
                state[cid]["last_updated"]        = datetime.now().isoformat()
                save_state(state_path, state)

                newly_processed.append((cand, filing, file_number, amendment_indicator))
                print(f"    Saved.")

            # Recompile every pass; notify only for new filings
            n = compile_csv(raw_dir, output_csv, state)
            if n:
                print(f"\n  Compiled {n} rows → {output_csv}")
                if args.sheet_id and os.path.exists(args.credentials):
                    try:
                        upload_to_sheets(output_csv, args.sheet_id, args.credentials, args.worksheet)
                        print(f"  Sheets updated.")
                    except Exception as e:
                        print(f"  Sheets upload failed: {e}")

                    if args.quarter == "Q2":
                        try:
                            overallspend.update_overallspend_chart(args.output_dir, args.sheet_id, args.credentials)
                            print(f"  overallspend_chart updated.")
                        except Exception as e:
                            print(f"  overallspend_chart update failed: {e}")

                        try:
                            groupspend.update_groupspend_chart(args.output_dir, args.sheet_id, args.credentials)
                            print(f"  groupspend_chart updated.")
                        except Exception as e:
                            print(f"  groupspend_chart update failed: {e}")

                        try:
                            groupspend.update_all_groups_chart(args.output_dir, args.sheet_id, args.credentials)
                            print(f"  groupspend_chart_ALL updated.")
                        except Exception as e:
                            print(f"  groupspend_chart_ALL update failed: {e}")
            if newly_processed:

                for cand, filing, file_number, amendment_indicator in newly_processed:
                    try:
                        notify(
                            candidate_name=cand["candidate_name"],
                            committee_name=cand["committee_name"],
                            amendment_indicator=amendment_indicator,
                        )
                        print(f"  Notification sent: {cand['candidate_name']}")
                    except Exception as e:
                        print(f"  Notification failed for {cand['candidate_name']}: {e}")

            # Check completion
            all_filed = all(state.get(c["committee_id"], {}).get("file_number") for c in candidates)
            if all_filed and args.stop_when_complete:
                print("\nAll candidates have filed. Exiting.")
                break

            if args.once:
                print("\nSingle pass complete.")
                break

            print(f"\nNext check in {args.poll_interval // 60}m {args.poll_interval % 60}s...\n")
            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print("\nStopped by user.")
        n = compile_csv(raw_dir, output_csv, state)
        print(f"Final compile: {n} rows → {output_csv}")
        if n and args.sheet_id and os.path.exists(args.credentials):
            try:
                upload_to_sheets(output_csv, args.sheet_id, args.credentials, args.worksheet)
                print("Sheets updated.")
            except Exception as e:
                print(f"Sheets upload failed: {e}")

            if args.quarter == "Q2":
                try:
                    overallspend.update_overallspend_chart(args.output_dir, args.sheet_id, args.credentials)
                    print("overallspend_chart updated.")
                except Exception as e:
                    print(f"overallspend_chart update failed: {e}")

                try:
                    groupspend.update_groupspend_chart(args.output_dir, args.sheet_id, args.credentials)
                    print("groupspend_chart updated.")
                except Exception as e:
                    print(f"groupspend_chart update failed: {e}")

                try:
                    groupspend.update_all_groups_chart(args.output_dir, args.sheet_id, args.credentials)
                    print("groupspend_chart_ALL updated.")
                except Exception as e:
                    print(f"groupspend_chart_ALL update failed: {e}")


if __name__ == "__main__":
    main()
