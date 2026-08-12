"""
senate_ad_spend.py

One-off, run-by-hand full-cycle breakdown of TV / Digital / Mail / Text
advertising spend for the four main MI Senate candidates (Stevens,
El-Sayed, McMorrow, Rogers), covering their whole campaign through the
Aug 4 2026 primary.

Why this doesn't just query api.open.fec.gov's schedules/schedule_b/
endpoint: confirmed directly that it has a real indexing gap for these
committees -- min_date=2026-04-01 (or later) returns ZERO records despite
their local pre-primary filings clearly showing large April-July
disbursements. Instead, this pulls each candidate's own filed reports
(Q1/Q2/Q3/YE 2025, Q1/Q2/12P 2026) via committee/{id}/filings/, downloads
and parses each one's itemized Schedule B (Line 17, Operating
Expenditures) directly via FastFEC -- same mechanism outside_spending.py
already uses for its RSS fast path -- and categorizes each line item by
its purpose description. FEC's structured category_code field is blank
on every row for all four campaigns (confirmed separately), so
categorization here is necessarily keyword-based on the free-text
purpose/description field, not a clean structured filter.

Not wired into any pipeline -- run manually:
    export FEC_API_KEY="..."
    python senate_ad_spend.py
"""

import csv
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

BASE_URL = "https://api.open.fec.gov/v1"
FEC_DOCQUERY_TMPL = "https://docquery.fec.gov/dcdev/posted/{file_number}.fec"
FASTFEC = "fastfec"

CANDIDATES = {
    "elsayed": "C00902668",
    "rogers": "C00849810",
    "mcmorrow": "C00901173",
    "stevens": "C00903039",
}
DISPLAY_NAME = {"elsayed": "El-Sayed", "rogers": "Rogers", "mcmorrow": "McMorrow", "stevens": "Stevens"}

MIN_COVERAGE_START = "2025-01-01"
MAX_DISBURSEMENT_DATE = "2026-08-04"  # primary day -- excludes Rogers'/El-Sayed's general-election spend

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", ".sb_filing_cache")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output")

# Category definitions and known limitations are documented in GUIDE.md
# ("Senate Ad Spend Categorization Methodology") -- read that before
# changing anything here, especially the two fixed bugs noted below.
#
# Checked in order, first match wins -- a purpose string that could match
# multiple categories (routine here: filers often bundle several
# services into one line item, e.g. "FUNDRAISING CONSULTING / MEDIA
# PRODUCTION / MEDIA PLACEMENT / SMS MESSAGES") gets its FULL dollar
# amount counted under whichever category is listed first below, not
# split proportionally -- there's no way to tell from the purpose text
# alone how a bundled dollar figure divides across the services named.
# Order reflects how confident/specific each category is: text/TV/
# digital/mail are checked first since those keywords are the most
# specific signal of an actual line item's true nature; the broader
# "could be almost anything" buckets are checked last.
CATEGORY_KEYWORDS = [
    ("Text/SMS", ["text messag", "sms", "texting service"]),
    ("TV", ["tv advertis", "television advertis", "broadcast advertis"]),
    ("Digital", ["digital advertis", "digital media", "digital consult", "digital services",
                 "digital market", "online advertis", "social media advertis"]),
    ("Mail", ["direct mail", "mail consult", "list rental", "postage"]),
    # _is_bundled_media() checked here, between Mail and Printing -- see
    # _categorize() below.
    ("Printing", ["printing"]),
    ("Communications/Social Media Consulting", ["communications consult", "social media consult"]),
    ("Digital Fundraising/List Consulting", ["fundraising", "list acquisition", "paid acquisition"]),
]


def _is_bundled_media(purpose_lower):
    """"Media Buy"/"Media Placement"/"Media Production"/"Ad Production"/
    "Video Production" -- costs of producing or placing an ad where the
    medium (TV vs. digital vs. other) isn't specified, so it can't be
    folded into TV or Digital without guessing. Word-presence check, not
    a literal phrase match: "MEDIA CONSULTING / PRODUCTION / PLACEMENT"
    (a real purpose string in this data) does NOT contain the literal
    substring "media production" or "media placement" -- the word
    "media" and the word "production"/"placement" are separated by
    "consulting" in between. A prior version of this function used
    literal phrase substrings and silently missed $501,198 across 2 rows
    for exactly this reason (Grant caught it 2026-08-11)."""
    has_media_and_action = "media" in purpose_lower and any(
        k in purpose_lower for k in ["buy", "placement", "production"])
    has_ad_or_video_production = any(
        k in purpose_lower for k in ["ad production", "ad buy", "ad placement", "video production"])
    return has_media_and_action or has_ad_or_video_production


def query_fec(endpoint, params, retries=6):
    params = dict(params)
    params["api_key"] = os.environ.get("FEC_API_KEY", "")
    url = f"{BASE_URL}/{endpoint}?" + urllib.parse.urlencode(params)
    import time
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
    window) -- an amendment restates the whole report, so only the
    highest file_number per window should be counted."""
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


def _download_sb17(file_number):
    """Downloads and parses one filing's Schedule B Line 17 (Operating
    Expenditures) rows via FastFEC, caching to disk by file_number."""
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
        print(f"    (no SB17 for file {file_number}, or FastFEC failed: {result.stderr[:200]})")
        return []

    rows = []
    with open(sb_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("memo_code") or "").strip().upper() == "X":
                continue
            rows.append({
                "date": row.get("expenditure_date"),
                "amount": row.get("expenditure_amount"),
                "purpose": (row.get("expenditure_purpose_descrip") or "").strip(),
                "category_code": row.get("category_code"),
                "payee": row.get("payee_organization_name") or
                         f"{row.get('payee_last_name', '')} {row.get('payee_first_name', '')}".strip(),
            })
    with open(cache_path, "w") as f:
        json.dump(rows, f)
    return rows


BUNDLED_MEDIA_LABEL = "Bundled Media (TV/digital/other, not separable)"


def _categorize(purpose):
    p = purpose.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if category == "Printing" and _is_bundled_media(p):
            # Bundled Media sits between Mail and Printing in priority --
            # checked here rather than as its own CATEGORY_KEYWORDS entry
            # since it's a function (word-presence check), not a keyword list.
            return BUNDLED_MEDIA_LABEL
        if any(k in p for k in keywords):
            return category
    return "Other/non-ad"


AD_RELATED_CATEGORIES = {name for name, _ in CATEGORY_KEYWORDS} | {BUNDLED_MEDIA_LABEL}


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def collect():
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    items = []
    for slug, committee_id in CANDIDATES.items():
        filings = _relevant_filings(committee_id)
        print(f"{DISPLAY_NAME[slug]}: {len(filings)} reports since {MIN_COVERAGE_START}")
        for filing in filings:
            file_number = filing["file_number"]
            print(f"  {filing['report_type']:<4} {filing.get('coverage_start_date')}..{filing.get('coverage_end_date')} "
                  f"file={file_number}", end=" ")
            rows = _download_sb17(file_number)
            print(f"-> {len(rows)} SB17 rows")
            for row in rows:
                date = row["date"] or ""
                if date > MAX_DISBURSEMENT_DATE:
                    continue
                amount = _to_float(row["amount"])
                items.append({
                    "Candidate": DISPLAY_NAME[slug],
                    "Date": date,
                    "Amount": amount,
                    "Payee": row["payee"],
                    "Purpose": row["purpose"],
                    "Category": _categorize(row["purpose"]),
                    "File Number": file_number,
                    "Report Type": filing["report_type"],
                })
    return items


# Display order for summary columns -- Bundled Media inserted between
# Mail and Printing to match its priority position in _categorize().
SUMMARY_CATEGORY_ORDER = ["Text/SMS", "TV", "Digital", "Mail", BUNDLED_MEDIA_LABEL, "Printing",
                          "Communications/Social Media Consulting", "Digital Fundraising/List Consulting"]


def summarize(items):
    totals = defaultdict(float)
    for r in items:
        totals[(r["Candidate"], r["Category"])] += r["Amount"]
    summary = []
    for slug in ("Stevens", "El-Sayed", "McMorrow", "Rogers"):
        row = {"Candidate": slug}
        ad_total = 0.0
        for category in SUMMARY_CATEGORY_ORDER:
            v = totals.get((slug, category), 0.0)
            row[category] = round(v, 2)
            ad_total += v
        other = totals.get((slug, "Other/non-ad"), 0.0)
        row["Other/non-ad"] = round(other, 2)
        row["Total ad-related"] = round(ad_total, 2)
        row["Total (all categories)"] = round(ad_total + other, 2)
        summary.append(row)
    return summary


def main():
    items = collect()

    items_path = os.path.join(OUTPUT_DIR, "senate_ad_spend_items.csv")
    with open(items_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Candidate", "Date", "Amount", "Payee", "Purpose",
                                                "Category", "File Number", "Report Type"])
        writer.writeheader()
        writer.writerows(items)
    print(f"\nWrote {len(items)} itemized rows -> {items_path}")

    summary = summarize(items)
    summary_path = os.path.join(OUTPUT_DIR, "senate_ad_spend_summary.csv")
    fieldnames = ["Candidate"] + SUMMARY_CATEGORY_ORDER + ["Other/non-ad", "Total ad-related", "Total (all categories)"]
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)
    print(f"Wrote summary -> {summary_path}\n")

    for row in summary:
        print(row)


if __name__ == "__main__":
    main()
