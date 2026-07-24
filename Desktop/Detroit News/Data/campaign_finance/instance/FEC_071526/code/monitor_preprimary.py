"""
monitor_preprimary.py

Tracks Michigan candidates' 12-day pre-primary reports (FEC report_type
"12P"), a one-time special report distinct from the regular quarterly
cycle -- due 12 days before a primary election (MI's 2026 primary is
August 4, so 12P reports are due July 23). Only QUARTERLY filers must
submit one; monthly filers are exempt (11 CFR 104.5), so most/all rows
here come from candidates who file quarterly.

Reuses monitor.py's FastFEC/compile/Sheets-upload machinery wholesale
(the F3N/F3A schema is identical regardless of report_type) but swaps
the "what counts as a match" logic from quarter/coverage_end_date
matching to a direct report_type == "12P" check -- a 12P's coverage
window is candidate-specific, not a fixed calendar date the way a
quarter is, so the quarter-matching approach in monitor.py doesn't
apply here.

Runs against a SEPARATE raw/state/output tree from monitor.py so it
can't interfere with the live Q2 quarterly tracker.

Usage:
    export FEC_API_KEY="..."
    python monitor_preprimary.py \\
        --candidates candidates.csv \\
        --output-dir '..' \\
        --worksheet cands_preprimary \\
        --once
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor import (
    BASE_URL, FEC_DOCQUERY_TMPL, COLUMNS_MAPPING, OUTPUT_COLUMNS,
    query_fec, run_fastfec, compile_csv, upload_to_sheets,
    notify, load_state, save_state,
)
import overallspend
import groupspend

REPORT_TYPE = "12P"

RSS_BASE_URL = "https://efilingapps.fec.gov/rss/generate"
RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
RSS_FIELDS_RE = re.compile(
    r"CommitteeId:\s*(?P<committee_id>[A-Z0-9]+)\s*\|\s*FilingId:\s*(?P<file_number>\d+)"
    r"\s*\|\s*FormType:\s*(?P<form_type>[A-Z0-9]+)"
    r"\s*\|\s*CoverageFrom:\s*(?P<coverage_from>[\d/]*)"
    r"\s*\|\s*CoverageThrough:\s*(?P<coverage_through>[\d/]*)"
    r"\s*\|\s*ReportType:\s*(?P<report_type>[^*]*)"
)
RELEVANT_FORM_TYPES = re.compile(r"^F3[NXA]")


def get_latest_pre_primary_filing(committee_id, api_key):
    """
    Mirrors monitor.get_latest_filing()'s shape, but matches on
    report_type == "12P" directly instead of a calendar-quarter end date
    -- a 12P's coverage window is candidate-specific (tied to each
    committee's own filing frequency and the shared primary date), so
    there's no single fixed coverage_end_date to match against the way
    QUARTER_END works for regular quarterly reports.
    """
    params = {
        "committee_id": committee_id,
        "sort": "-coverage_end_date",
        "per_page": 10,
    }
    data = query_fec("reports/house-senate/", params, api_key)
    results = data.get("results", [])
    matches = [r for r in results if r.get("report_type") == REPORT_TYPE]
    if not matches:
        return None
    return max(matches, key=lambda r: r.get("file_number", 0))


def fetch_rss_pre_primary_force_ids(committee_ids):
    """
    Like monitor.fetch_rss_force_ids(), but matches on the RSS feed's
    ReportType text ("PRE-PRIMARY") since a 12P's FormType is just plain
    "F3N"/"F3A" (indistinguishable from a regular quarterly report at
    that level) and its CoverageThrough varies by committee -- confirmed
    via Lulgjuraj's real 12P filing (RSS ReportType: "PRE-PRIMARY").
    Non-fatal on error -- speed optimization, never a hard dependency.
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

    force_ids = {}
    for item in RSS_ITEM_RE.findall(body):
        m = RSS_FIELDS_RE.search(item)
        if not m:
            continue
        cid = m.group("committee_id")
        if cid in force_ids:
            continue
        if not RELEVANT_FORM_TYPES.match(m.group("form_type")):
            continue
        if "PRE-PRIMARY" not in m.group("report_type").upper():
            continue
        form_type = m.group("form_type")
        is_amendment = "A" in form_type[2:]
        force_ids[cid] = (int(m.group("file_number")), is_amendment)
    return force_ids


def verify_pre_primary_coverage(candidate_dir, file_number):
    """
    Ground-truth check on the downloaded filing itself, mirroring
    monitor.verify_filing_coverage()'s role: confirm report_code == "12P"
    directly out of the F3N/F3A CSV, regardless of whether the
    file_number came from the API or RSS. Rejects and discards if not --
    never trust a file_number alone.
    """
    folder = os.path.join(candidate_dir, str(file_number))
    for name in ("F3N.csv", "F3A.csv"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            try:
                with open(path, newline="", encoding="utf-8") as f:
                    row = next(csv.DictReader(f), None)
                return bool(row) and row.get("report_code") == REPORT_TYPE
            except Exception:
                return False
    return False


def main():
    parser = argparse.ArgumentParser(description="Track 12-day pre-primary reports.")
    parser.add_argument("--candidates", default="candidates.csv")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cycle", type=int, default=2026)
    parser.add_argument("--poll-interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sheet-id", default="10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc")
    parser.add_argument("--worksheet", default="cands_preprimary")
    parser.add_argument("--credentials", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "app-template-access-402821-3111eabfc82d.json"))
    parser.add_argument("--force", action="append", metavar="COMMITTEE_ID:FILE_NUMBER")
    parser.add_argument("--no-rss", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("FEC_API_KEY", "")
    if not api_key:
        sys.exit("Error: FEC_API_KEY environment variable not set.")

    # Separate tree from monitor.py's raw/.monitor_state.json -- a 12P's
    # file_number could otherwise sit alongside a candidate's Q2 filing in
    # the same folder and get mistaken for "the latest quarterly data" by
    # monitor.py's highest-file_number-wins fallback.
    raw_dir = os.path.join(args.output_dir, "raw_preprimary")
    output_csv = os.path.join(args.output_dir, "output", f"campaign_finance_{args.cycle}_preprimary.csv")
    state_path = os.path.join(args.output_dir, ".monitor_preprimary_state.json")

    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(os.path.join(args.output_dir, "output"), exist_ok=True)

    with open(args.candidates, newline="", encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    print(f"Loaded {len(candidates)} candidates.")
    print(f"Monitoring {args.cycle} pre-primary (12P) reports | output: {args.output_dir}")
    print(f"Poll interval: {args.poll_interval}s\n")

    state = load_state(state_path)
    for cand in candidates:
        cid = cand["committee_id"]
        if cid not in state:
            state[cid] = {}
        state[cid]["candidate_name"] = cand["candidate_name"]
        state[cid]["contest_id"] = cand["contest_id"]
        state[cid]["committee_name"] = cand["committee_name"]
        state[cid]["party"] = cand.get("party", "OTH")
    save_state(state_path, state)

    try:
        while True:
            with open(args.candidates, newline="", encoding="utf-8") as f:
                candidates = list(csv.DictReader(f))
            for cand in candidates:
                cid = cand["committee_id"]
                if cid not in state:
                    state[cid] = {}
                state[cid]["candidate_name"] = cand["candidate_name"]
                state[cid]["contest_id"] = cand["contest_id"]
                state[cid]["committee_name"] = cand["committee_name"]
                state[cid]["party"] = cand.get("party", "OTH")
                state[cid]["committee_id"] = cid
                state[cid]["first_name"] = cand.get("first_name", "")

            force_map = {}
            if not args.no_rss:
                force_map.update(fetch_rss_pre_primary_force_ids(
                    [c["committee_id"] for c in candidates]))
            if args.force:
                for entry in args.force:
                    if ":" in entry:
                        cid, fnum = entry.split(":", 1)
                        try:
                            force_map[cid] = (int(fnum), False)
                        except ValueError:
                            pass

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            filed_count = sum(1 for c in candidates if state.get(c["committee_id"], {}).get("file_number"))
            print(f"[{now}] {filed_count}/{len(candidates)} filed 12P — checking {len(candidates) - filed_count} pending...")

            newly_processed = []

            for cand in candidates:
                cid = cand["committee_id"]
                try:
                    filing = get_latest_pre_primary_filing(cid, api_key)
                except Exception as e:
                    print(f"  {cand['candidate_name']}: API error — {e}")
                    time.sleep(0.5)
                    continue
                time.sleep(0.2)

                if not filing:
                    forced = force_map.get(cid)
                    if forced:
                        file_number, is_amendment = forced
                        amendment_indicator = "A" if is_amendment else "N"
                        print(f"  {cand['candidate_name']:<20} no API filing — using RSS/force #{file_number}")
                    else:
                        print(f"  {cand['candidate_name']:<20} no 12P yet")
                        continue
                else:
                    file_number = filing.get("file_number")
                    amendment_indicator = filing.get("amendment_indicator", "N")
                last_file_number = state[cid].get("file_number")

                if file_number == last_file_number:
                    label = "Amendment" if amendment_indicator == "A" else "Original"
                    print(f"  {cand['candidate_name']:<20} already current (#{file_number}, {label})")
                    continue

                is_update = last_file_number is not None
                action = "AMENDMENT" if is_update else "NEW FILING"
                print(f"  {cand['candidate_name']:<20} {action} #{file_number} — downloading...")

                party = state[cid].get("party", "OTH")
                candidate_dir = os.path.join(raw_dir, cand["contest_id"], party, cand["candidate_name"])
                try:
                    run_fastfec(file_number, candidate_dir)
                except Exception as e:
                    print(f"    FastFEC error: {e}")
                    continue

                if not verify_pre_primary_coverage(candidate_dir, file_number):
                    print(f"    REJECTED: #{file_number} is not a 12P pre-primary report -- discarding.")
                    shutil.rmtree(os.path.join(candidate_dir, str(file_number)), ignore_errors=True)
                    continue

                state[cid]["file_number"] = file_number
                state[cid]["amendment_indicator"] = amendment_indicator
                state[cid]["last_updated"] = datetime.now().isoformat()
                save_state(state_path, state)

                newly_processed.append((cand, filing, file_number, amendment_indicator))
                print(f"    Saved.")

            n = compile_csv(raw_dir, output_csv, state)
            if n:
                print(f"\n  Compiled {n} rows → {output_csv}")
                if args.sheet_id and os.path.exists(args.credentials):
                    try:
                        upload_to_sheets(output_csv, args.sheet_id, args.credentials, args.worksheet)
                        print("  Sheets updated.")
                    except Exception as e:
                        print(f"  Sheets upload failed: {e}")

                    try:
                        overallspend.update_overallspend_chart(args.output_dir, args.sheet_id, args.credentials)
                        print("  overallspend_chart updated.")
                    except Exception as e:
                        print(f"  overallspend_chart update failed: {e}")

                    try:
                        groupspend.update_all_groups_chart(args.output_dir, args.sheet_id, args.credentials)
                        print("  groupspend_chart_ALL updated.")
                    except Exception as e:
                        print(f"  groupspend_chart_ALL update failed: {e}")
            for cand, filing, file_number, amendment_indicator in newly_processed:
                try:
                    notify(candidate_name=cand["candidate_name"],
                           committee_name=cand["committee_name"],
                           amendment_indicator=amendment_indicator)
                    print(f"  Notification sent: {cand['candidate_name']}")
                except Exception as e:
                    print(f"  Notification failed for {cand['candidate_name']}: {e}")

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

            try:
                overallspend.update_overallspend_chart(args.output_dir, args.sheet_id, args.credentials)
                print("overallspend_chart updated.")
            except Exception as e:
                print(f"overallspend_chart update failed: {e}")

            try:
                groupspend.update_all_groups_chart(args.output_dir, args.sheet_id, args.credentials)
                print("groupspend_chart_ALL updated.")
            except Exception as e:
                print(f"groupspend_chart_ALL update failed: {e}")


if __name__ == "__main__":
    main()
