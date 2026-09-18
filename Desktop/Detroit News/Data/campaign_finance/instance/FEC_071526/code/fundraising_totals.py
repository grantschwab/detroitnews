"""
fundraising_totals.py

Whole-cycle campaign fundraising breakdown for the two MI Senate general
candidates (El-Sayed C00902668, Rogers C00849810): Itemized (Michigan),
Itemized (out of state), Small dollar (unitemized), Other. Pushed to the
"SEN_fundraising_totals" tab in the same Flourish-facing graphics
spreadsheet as the rest of this project's Senate tabs.

WHY THIS NEEDED A NEW MODULE, NOT JUST A NEW COLUMN ON THE EXISTING
campaign_finance_2026_Q2.csv: monitor.py only pulls F3 summary-page
totals (col_a_*/col_b_* fields) -- itemized-vs-unitemized and state-level
detail aren't in that data at all. Getting this requires the same
itemized-Schedule-A FastFEC pull rogers_abdul_landingpage/donor_maxout.py
already does for individual donor tracking -- but that script only reads
SA11AI.csv (Line 11(a)(i), direct itemized individual contributions), and
silently misses money routed through a joint fundraising committee (JFC).

THE JFC MECHANISM (confirmed directly against real cached FastFEC output
for Rogers' committee, not guessed): a JFC transfer -- e.g. "Team
Rogers" / "One Team Senate Majority" -- is filed under Schedule A Line 12
("Transfers from Authorized/Joint Fundraising Committees", SA12.csv, same
column schema as SA11AI.csv) as a paired block: one summary row
(entity_type=PAC/COM/ORG, contributor_organization_name = the JFC,
memo_text_description="TRANSFER OF JOINT FUNDRAISING PROCEEDS"), followed
by individual entity_type=IND breakdown rows linked via
back_reference_tran_id_number, each carrying the REAL donor's name,
contributor_state, and their attributed amount
(memo_text_description like "JFC ATTRIB: ONE TEAM SENATE MAJORITY").
This is structurally the same conduit pattern donor_maxout.py already
handles correctly for ActBlue/WinRed earmarking -- filtering to
entity_type=="IND" across BOTH SA11AI.csv and SA12.csv naturally
captures JFC-routed money by state, with no separate JFC-filing lookup
needed. Confirmed: SA11B (party contributions), SA11C (PAC
contributions), SA14/SA15 (loans) never carry entity_type=IND in the
cached data, so none of those belong in the itemized buckets -- they
(correctly) fall into "Other" below.

KNOWN WRINKLE, HANDLED BY DESIGN, NOT RECONCILED: a JFC summary row's
transfer amount doesn't always exactly equal the sum of its linked
breakdown rows (confirmed on a real example: $3,139.03 transferred vs.
$3,500 + -$238.10 = $3,261.90 attributed) -- JFCs allocate each donor's
money across participating committees by a formula, not 1:1. Rather than
trying to reconcile this, "Other" is computed as a REMAINDER
(col_b_total_receipts minus the other three buckets), so any such
rounding -- plus real non-individual receipts (PAC/party contributions,
loans) -- lands there automatically. No double-counting risk either way.

Only 6 of ~162,000 checked IND rows (across both candidates' full
cached history) have a blank contributor_state -- negligible, bucketed
into "out of state" by default.

col_b_* fields (e.g. col_b_total_receipts,
col_b_individual_contributions_unitemized) are already cycle-to-date
cumulative on each filed report -- BUT this module does NOT read
col_b_total_receipts off the raw filing (an earlier version did).
Confirmed real 2026-09-21: Rogers' own filed col_b_total_receipts
doesn't match api.open.fec.gov's independently-recomputed totals for
the same committee/period -- a filer-side self-reported-top-line
inconsistency (his own numbers don't fully reconcile), not a parsing
bug. See _committee_totals()'s docstring for the full story, including
how this was caught (Grant compared against FEC.gov's own displayed
total) and why El-Sayed's number was unaffected (his raw field happened
to match exactly). Total and Small dollar (unitemized) both now come
from committee/{id}/totals/ instead, FEC's own authoritative aggregate.

Not yet observed in the cached data: El-Sayed has zero JFC-routed
contributions as of this writing (he may not be using a joint
fundraising committee, or just hasn't had one show up yet) -- this may
change as the cycle progresses; no code change needed if it does, since
the SA12.csv parsing applies to both candidates identically.

Called at the end of outside_spending.py's own sheet-upload step.
"""

import csv
import json
import os
import subprocess
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

BASE_URL = "https://api.open.fec.gov/v1"
FEC_DOCQUERY_TMPL = "https://docquery.fec.gov/dcdev/posted/{file_number}.fec"
FASTFEC = "fastfec"

CANDIDATES = {"elsayed": "C00902668", "rogers": "C00849810"}
DISPLAY_NAME = {"elsayed": "El-Sayed", "rogers": "Rogers"}

MIN_COVERAGE_START = "2025-01-01"  # start of the 2025-2026 cycle -- same
                                    # cutoff convention used throughout
                                    # this project (senate_ad_spend.py,
                                    # donor_maxout.py), excludes Rogers'
                                    # committee's pre-2025 (2024 cycle)
                                    # filings under the same committee ID.

GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"
OUTPUT_COLUMNS = ["Candidate", "Itemized (Michigan)", "Itemized (out of state)",
                  "Small dollar (unitemized)", "Other", "Total"]

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".fundraising_cache")


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


def _relevant_filings(committee_id):
    """Every F3 report for this committee covering MIN_COVERAGE_START or
    later, deduped to the latest amendment per (report_type, coverage
    window). Identical logic to senate_ad_spend.py's/postprim_chart.py's
    own filing-selection helpers, copied rather than imported since each
    of these one-off/sibling modules is meant to stand alone."""
    data = query_fec(f"committee/{committee_id}/filings/", {"per_page": 100, "sort": "-coverage_end_date"})
    best = {}
    for r in data.get("results", []):
        if r.get("form_type") != "F3" or not r.get("report_type"):
            continue
        start = r.get("coverage_start_date") or ""
        if start < MIN_COVERAGE_START:
            continue
        key = (r["report_type"], r.get("coverage_start_date"), r.get("coverage_end_date"))
        if key not in best or r["file_number"] > best[key]["file_number"]:
            best[key] = r
    return sorted(best.values(), key=lambda r: r.get("coverage_start_date") or "")


def _download_filing(file_number):
    """Downloads and parses one filing's SA11AI.csv and SA12.csv rows via
    FastFEC, caching to disk by file_number. Returns just the itemized
    entity_type=IND rows -- NOT the filing's own self-reported summary
    totals (see _committee_totals() for why)."""
    cache_path = os.path.join(CACHE_DIR, f"{file_number}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    tmp_dir = os.path.join(CACHE_DIR, "_tmp", str(file_number))
    os.makedirs(tmp_dir, exist_ok=True)
    url = FEC_DOCQUERY_TMPL.format(file_number=file_number)
    cmd = f'curl -s "{url}" | {FASTFEC} -s {file_number} "{tmp_dir}/"'
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    filing_dir = os.path.join(tmp_dir, str(file_number))

    ind_rows = []
    for schedule in ("SA11AI.csv", "SA12.csv"):
        sched_path = os.path.join(filing_dir, schedule)
        if not os.path.exists(sched_path):
            continue
        with open(sched_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("entity_type") or "").strip().upper() != "IND":
                    continue
                ind_rows.append({
                    "state": (row.get("contributor_state") or "").strip().upper(),
                    "amount": row.get("contribution_amount"),
                })

    if result.returncode != 0 and not ind_rows:
        print(f"    (FastFEC failed for file {file_number}: {result.stderr[:200]})")

    with open(cache_path, "w") as f:
        json.dump(ind_rows, f)
    return ind_rows


def _committee_totals(committee_id):
    """Total receipts and unitemized individual contributions, straight
    from api.open.fec.gov's own committee/{id}/totals/ endpoint -- NOT
    read off the raw filing's self-reported col_b_total_receipts field.

    Confirmed real 2026-09-21 (Grant flagged Rogers' number as "slightly
    off" vs. FEC.gov's own displayed total): Rogers' own 12P filing's
    col_b_total_receipts field ($10,945,823.94-ish) does NOT match
    api.open.fec.gov's independently-recomputed "receipts" aggregate for
    the same committee/period ($10,833,276.21) -- a filer-side
    self-reported-top-line inconsistency, not a bug in how this script
    reads the field (the same field-reading code produced a byte-exact
    match against the totals endpoint for El-Sayed, $14,514,335.93 both
    ways, so the raw field is usually fine -- just not reliable enough to
    trust blindly). The totals endpoint is FEC's own authoritative
    recomputed number and is what FEC.gov's committee page is built on,
    so it's used here instead.

    A small residual gap (~$65k for Rogers, well under 1%) can still
    exist between this and FEC.gov's live page. TESTED AND RULED OUT
    2026-09-21: Form 6 (48-hour notice) contributions filed since the
    last periodic report -- Rogers had 13 such filings since his 12P
    report (not just the 9 originally spotted), totaling ~$217,649, all
    confirmed non-amended and non-duplicate (checked amendment_indicator
    and per-transaction date/amount/state). Adding that in looked
    promising for Rogers alone, but applying the exact same logic to
    El-Sayed (whose Total was independently confirmed correct against
    FEC.gov) would have pushed his number from $14,514,336 to
    $15,385,463 -- i.e. WRONG. That disproves the hypothesis: FEC.gov's
    displayed "total raised" does NOT simply add unreported Form 6 money
    on top of the last periodic report. Whatever accounts for the
    remaining small gap, it isn't this -- don't re-attempt the Form 6
    approach without a new theory first."""
    data = query_fec(f"committee/{committee_id}/totals/", {"cycle": 2026, "per_page": 5})
    results = data.get("results", [])
    if not results:
        return 0.0, 0.0
    r = results[0]
    return _to_float(r.get("receipts")), _to_float(r.get("individual_unitemized_contributions"))


def _candidate_totals(committee_id):
    filings = _relevant_filings(committee_id)

    itemized_mi = 0.0
    itemized_oos = 0.0
    for filing in filings:
        file_number = filing["file_number"]
        print(f"  {filing['report_type']:<4} {filing.get('coverage_start_date')}..{filing.get('coverage_end_date')} "
              f"file={file_number}", end=" ")
        ind_rows = _download_filing(file_number)
        print(f"-> {len(ind_rows)} itemized IND rows")
        for row in ind_rows:
            amount = _to_float(row["amount"])
            if row["state"] == "MI":
                itemized_mi += amount
            else:
                itemized_oos += amount

    total_receipts, unitemized = _committee_totals(committee_id)
    other = total_receipts - itemized_mi - itemized_oos - unitemized

    return {
        "Itemized (Michigan)": round(itemized_mi, 2),
        "Itemized (out of state)": round(itemized_oos, 2),
        "Small dollar (unitemized)": round(unitemized, 2),
        "Other": round(other, 2),
        "Total": round(total_receipts, 2),
    }


def build_rows():
    os.makedirs(CACHE_DIR, exist_ok=True)
    rows = []
    for slug, committee_id in CANDIDATES.items():
        print(f"{DISPLAY_NAME[slug]}:")
        totals = _candidate_totals(committee_id)
        rows.append({"Candidate": DISPLAY_NAME[slug], **totals})
    return rows


def update(output_dir, credentials_path, worksheet_name="SEN_fundraising_totals"):
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
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=20, cols=10)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    last_col = chr(64 + len(OUTPUT_COLUMNS))
    ws.format(f"A1:{last_col}1", {"textFormat": {"bold": True}})
    ws.format(f"B2:{last_col}{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    return rows


if __name__ == "__main__":
    rows = build_rows()
    for row in rows:
        print(row)
