"""
outside_spending.py

Tracks independent expenditures (FEC Schedule E) by outside groups --
Super PACs, party committees, anyone -- supporting or opposing the
candidates in candidates.csv. Unlike committee-filed reports, this needs
no group names up front.

Queries Schedule E by CONTEST (state + office + district), not by a
specific candidate_id. Many filers -- e.g. "Fighting for Michigan PAC"
spending for Abdul El-Sayed -- only fill in the free-text candidate name
on their Schedule E filing and leave the structured candidate_id link
blank. Querying by candidate_id alone silently misses those. Contest
fields (candidate_office_state/office/district) are populated far more
reliably, and results are then matched back to candidates.csv rows by
last name.

Polls the FEC API every --poll-interval seconds (like monitor.py), fully
recomputing totals each cycle (rather than incrementally accumulating).
Includes both notice (24/48-hour) and periodic (F3X/F5) filings -- early
in a cycle big spenders often have ONLY filed a notice -- and dedupes the
overlap itself once a periodic report supersedes a notice for the same
expenditure (`most_recent=true` alone only resolves amendments of a
single filing, not this notice/periodic overlap).

api.open.fec.gov itself lags the FEC's own real-time systems by up to a
day or more, especially under heavy filing volume (e.g. the 20-day
pre-election window when 24/48-hour notices become mandatory). To close
that gap, each cycle also polls the FEC Electronic Filing RSS feed
(https://efilingapps.fec.gov/rss/generate?cids=...) for known outside
spenders, and for any new Schedule-E-bearing filing it hasn't seen,
downloads and parses it directly via FastFEC -- bypassing api.open.fec.gov
entirely for that filing. Those FastFEC-sourced rows are merged into the
same per-contest transaction list as the API results and pass through the
same dedupe_notice_vs_periodic() content-key dedup, so once the API
eventually indexes the same filing, the duplicate collapses to one entry
rather than double-counting.

Usage:
    export FEC_API_KEY="..."
    python outside_spending.py \\
        --candidates candidates.csv \\
        --output-dir '..' \\
        --cycle 2026 \\
        --worksheet "Outside Spending"
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

import overallspend
import groupspend

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

BASE_URL = "https://api.open.fec.gov/v1"
FEC_DOCQUERY_TMPL = "https://docquery.fec.gov/dcdev/posted/{file_number}.fec"
RSS_BASE_URL = "https://efilingapps.fec.gov/rss/generate"
FASTFEC = "fastfec"

OUTPUT_COLUMNS = [
    "Candidate Name", "First Name", "Contest ID", "Party",
    "Support/Oppose", "Outside Group",
    "2025 Spent", "Q1 2026 Spent", "Q2 2026 Spent", "Since Jul 1 Spent",
    "SUM CandCategory", "SUM GroupAll", "SELFREPORT YTD Spend",
    "# Transactions", "Most Recent Expenditure",
    "FEC Committee Link", "FEC Most Recent Report Link",
]

# Fixed period boundaries, ISO date strings sort lexicographically so
# plain string comparison against expenditure_date is safe.
PERIOD_BOUNDS = {
    "2025 Spent":        ("2025-01-01", "2025-12-31"),
    "Q1 2026 Spent":     ("2026-01-01", "2026-03-31"),
    "Q2 2026 Spent":     ("2026-04-01", "2026-06-30"),
    "Since Jul 1 Spent": ("2026-07-01", None),
}


# ── FEC API helpers ─────────────────────────────────────────────────────────

def query_fec(endpoint, params=None, retries=6):
    if params is None:
        params = {}
    params["api_key"] = os.environ.get("FEC_API_KEY", "")
    query = urllib.parse.urlencode(params, doseq=True)
    url = f"{BASE_URL}/{endpoint}?{query}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(min(3 * (2 ** attempt), 30))  # exponential backoff, capped at 30s
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
            else:
                raise
    return {}


def slugify(text):
    """Lowercase, alnum only -- matches preprocess.py's candidate_name slugging."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def contest_key(contest_id):
    """'mi00' -> ('MI', 'S', None); 'mi03' -> ('MI', 'H', '03')."""
    state = contest_id[:2].upper()
    district = contest_id[2:]
    office = "S" if district == "00" else "H"
    return (state, office, None if office == "S" else district)


def fetch_schedule_e_for_contest(state, office, district, cycle, min_date, max_pages=30):
    """
    Pull all most-recent Schedule E records for an entire contest (race),
    not a single candidate_id -- catches filers who only populated the
    free-text candidate name/office fields, not the structured candidate_id.
    """
    results = []
    last_index = None
    last_date = None
    for _ in range(max_pages):
        params = {
            "candidate_office_state": state,
            "candidate_office": office,
            "cycle": cycle,
            "min_date": min_date,
            "per_page": 100,
            "sort": "-expenditure_date",
            "most_recent": "true",
        }
        if district is not None:
            params["candidate_office_district"] = district
        if last_index is not None:
            params["last_index"] = last_index
            params["last_expenditure_date"] = last_date
        data = query_fec("schedules/schedule_e/", params)
        page = data.get("results", [])
        results.extend(page)
        pagination = data.get("pagination", {})
        indexes = pagination.get("last_indexes") or {}
        if len(page) < 100 or not indexes.get("last_index"):
            break
        last_index = indexes["last_index"]
        last_date = indexes["last_expenditure_date"]
        time.sleep(0.2)
    return results


# ── FEC Electronic Filing RSS feed + FastFEC fast path ──────────────────────
#
# api.open.fec.gov can lag the FEC's own systems by a day or more under
# heavy volume. This feed is the same real-time source FEC Notify emails
# are built on. We can't query it by contest (only by known committee ID),
# so it only accelerates spenders we've already seen at least once via the
# API -- it doesn't help discover a brand-new spender faster than the API
# contest query does. That's an acceptable scope: catching a KNOWN big
# spender's next filing quickly matters far more than shaving time off
# first-ever discovery.

RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
RSS_FIELDS_RE = re.compile(
    r"CommitteeId:\s*(?P<committee_id>[A-Z0-9]+)\s*\|\s*FilingId:\s*(?P<file_number>\d+)"
    r"\s*\|\s*FormType:\s*(?P<form_type>[A-Z0-9]+)"
)
# 24/48-hour IE notices (F24/F5) and PAC/party periodic reports (F3X),
# which are the form families that can carry a Schedule E (SE) schedule.
SE_RELEVANT_FORM_TYPES = re.compile(r"^F(24|5|3X)")


def fetch_rss_filings(committee_ids, chunk_size=150):
    """
    Returns (committee_id, file_number, form_type) tuples for Schedule-E-
    bearing filings in the last 7 days, for the given committee IDs.
    Non-fatal on error -- this is a speed optimization, never a hard
    dependency; the API contest query is still the source of truth.
    """
    if not committee_ids:
        return []
    results = []
    ids = sorted(committee_ids)
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        try:
            url = f"{RSS_BASE_URL}?{urllib.parse.urlencode({'cids': ','.join(chunk)})}"
            with urllib.request.urlopen(url, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  RSS feed fetch failed (non-fatal): {e}")
            continue
        for item in RSS_ITEM_RE.findall(body):
            m = RSS_FIELDS_RE.search(item)
            if not m or not SE_RELEVANT_FORM_TYPES.match(m.group("form_type")):
                continue
            results.append((m.group("committee_id"), int(m.group("file_number")), m.group("form_type")))
    return results


def parse_se_filing(file_number, form_type, cache_dir):
    """
    Downloads and parses one filing's Schedule E rows via FastFEC, caching
    the result to disk (keyed by file_number) so repeat polls don't
    re-download/re-parse. Returns a list of transaction dicts shaped like
    the ones aggregate() already consumes from the API, or [] on any
    failure -- never raises, since a bad filing shouldn't kill the cycle.
    """
    cache_path = os.path.join(cache_dir, f"{file_number}.json")
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    is_notice = not form_type.startswith("F3X")
    tmp_dir = os.path.join(cache_dir, "_tmp", str(file_number))
    try:
        os.makedirs(tmp_dir, exist_ok=True)
        url = FEC_DOCQUERY_TMPL.format(file_number=file_number)
        cmd = f'curl -s "{url}" | {FASTFEC} -s {file_number} "{tmp_dir}/"'
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        se_path = os.path.join(tmp_dir, str(file_number), "SE.csv")
        if result.returncode != 0 or not os.path.exists(se_path):
            return []

        transactions = []
        with open(se_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("memo_code") or "").strip().upper() == "X":
                    continue
                payee = row.get("payee_organization_name") or \
                    f"{row.get('payee_last_name', '')} {row.get('payee_first_name', '')}".strip()
                transactions.append({
                    "committee": {"committee_id": row.get("filer_committee_id_number")},
                    "support_oppose_indicator": row.get("support_oppose_code"),
                    "expenditure_amount": row.get("expenditure_amount"),
                    "expenditure_date": row.get("disbursement_date") or row.get("dissemination_date"),
                    "payee_name": payee,
                    "memoed_subtotal": False,
                    "is_notice": is_notice,
                    "candidate_last_name": row.get("candidate_last_name"),
                    "candidate_office": row.get("candidate_office"),
                    "candidate_state": row.get("candidate_state"),
                    "candidate_district": row.get("candidate_district"),
                    "office_total_ytd": row.get("calendar_y_t_d_per_election_office"),
                    "file_number": file_number,
                })
    except Exception as e:
        print(f"  FastFEC parse of filing {file_number} failed (non-fatal): {e}")
        return []

    with open(cache_path, "w") as f:
        json.dump(transactions, f)
    return transactions


def se_contest_key(txn):
    """Derive the same (state, office, district) shape as contest_key() from a parsed SE row."""
    state = (txn.get("candidate_state") or "").strip().upper()
    office = (txn.get("candidate_office") or "").strip().upper()
    district = (txn.get("candidate_district") or "").strip()
    if office == "H" and district:
        district = district.zfill(2)
    else:
        district = None
    return (state, office, district)


def get_committee_name(committee_id, cache):
    if committee_id in cache:
        return cache[committee_id]
    try:
        data = query_fec(f"committee/{committee_id}/", {"per_page": 1})
        results = data.get("results", [])
        name = results[0]["name"] if results else committee_id
    except Exception:
        name = committee_id
    cache[committee_id] = name
    return name


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


# ── Aggregation ──────────────────────────────────────────────────────────────

def dedupe_notice_vs_periodic(transactions):
    """
    A 24/48-hour notice and a later periodic (F3X/F5) report can both
    describe the same expenditure as separate transactions -- most_recent
    only resolves amendments of the *same* filing, not this overlap.

    Two passes with different jobs -- a single combined key/preference rule
    can't handle both:

    Pass 1 -- collapse cross-source duplicates using FEC's own `sub_id`
    (a guaranteed-unique transaction identifier, present on every
    api.open.fec.gov record) as the real signal, NOT content matching
    alone. Content (committee/candidate/payee/amount/date) is only used to
    catch a record that came from our RSS/FastFEC fast path (which has no
    sub_id -- FastFEC-parsed CSVs don't carry it) once the API has
    indexed an equivalent record with a real sub_id.

    Do NOT collapse same-content records purely because they share
    payee/amount/date/is_notice -- a single filing can legitimately
    itemize two genuinely separate line items with identical payee,
    amount, and date (confirmed on AFP Action/Rogers on 2026-07-21: two
    real, distinct $14,000 "People Who Think" charges both dated 2026-06-05
    in the same filing, each with its own sub_id). An earlier version of
    this function collapsed those into one and undercounted. Two records
    with different real sub_ids are always both real and both kept,
    however identical their content looks.

    Also deliberately does NOT prefer is_notice=False on a content+date
    match -- doing so previously caused a related bug: when an RSS-sourced
    periodic restatement happened to land on the exact same date as its
    notice, "prefer periodic" silently discarded the genuine notice
    record. Confirmed same incident via a $44,906.50 "People Who Think"
    payment dated 2026-06-25 on both records.

    Pass 2 -- a monthly/quarterly periodic filer's F3X report routinely
    RESTATES a transaction already disclosed via notice, but re-dated to
    somewhere in that report's coverage period, not necessarily near the
    original date (confirmed same incident: notice dated 2/02, periodic
    restatement of the same $500,000/payee dated 2/25 -- 23 days apart, far
    outside any reasonable fixed date-tolerance window, so this can't be
    folded into pass 1's exact-date key). For any is_notice=False record
    whose (committee, candidate, support/oppose, payee, amount) matches an
    is_notice=True record ANYWHERE in the group -- regardless of date --
    drop the periodic one as a restatement. Always keeps the notice, never
    the periodic version, when both exist. Deliberately does NOT merge
    is_notice=True records with each other even on an amount/payee match,
    since two genuinely separate real disbursements to the same vendor for
    the same round amount (a recurring media buy, e.g. $500,000 to the same
    vendor seven times over the cycle) are common and must stay separate --
    only a notice/periodic pairing is treated as evidence of duplication.

    Candidate + support/oppose MUST be part of the key: a single ad buy
    can legitimately be reported twice in one filing at the same
    committee/payee/amount/date -- once supporting one candidate, once
    opposing another (e.g. a joint contrast ad) -- and those are two real
    transactions, not duplicates of each other.

    Payee name is run through slugify() (strip all punctuation/whitespace,
    not just case) before keying -- api.open.fec.gov and FastFEC-parsed
    filings format the same payee differently (e.g. "IN PURSUIT OF LLC" vs
    "IN PURSUIT OF, LLC").
    """
    def content_key(t):
        committee = t.get("committee") or {}
        return (
            committee.get("committee_id"),
            slugify(t.get("candidate_last_name")),
            t.get("support_oppose_indicator"),
            slugify(t.get("payee_name")),
            round(float(t.get("expenditure_amount") or 0), 2),
        )

    # Pass 1: sub_id is FEC's own guaranteed-unique transaction identifier
    # (present on every api.open.fec.gov record; absent on FastFEC-parsed
    # RSS-sourced ones). Two different real sub_ids are always two real,
    # separate transactions -- keep both regardless of matching content.
    # Only drop a sub_id-less (RSS-sourced) record when an equivalent
    # sub_id-having (API-sourced) record already covers the same
    # content+date+is_notice -- it's redundant, the API already has it.
    with_sub_id = [t for t in transactions if t.get("sub_id")]
    without_sub_id = [t for t in transactions if not t.get("sub_id")]

    seen_ids = set()
    pass1 = []
    for t in with_sub_id:
        if t["sub_id"] in seen_ids:
            continue
        seen_ids.add(t["sub_id"])
        pass1.append(t)

    covered = {content_key(t) + (t.get("expenditure_date"), bool(t.get("is_notice")))
               for t in pass1}
    for t in without_sub_id:
        key = content_key(t) + (t.get("expenditure_date"), bool(t.get("is_notice")))
        if key not in covered:
            covered.add(key)
            pass1.append(t)

    # Pass 2: drop periodic restatements of an already-counted notice,
    # regardless of date. Notices always win.
    notice_keys = {content_key(t) for t in pass1 if t.get("is_notice")}
    result = [t for t in pass1 if t.get("is_notice") or content_key(t) not in notice_keys]
    return result


def aggregate(candidates, cycle, min_date, output_dir, use_rss=True):
    # Group tracked candidates by contest, keyed by last-name slug for matching.
    by_contest = defaultdict(dict)
    candidates_by_committee = {}
    for cand in candidates:
        key = contest_key(cand["contest_id"])
        by_contest[key][slugify(cand["candidate_name"])] = cand
        candidates_by_committee[cand["committee_id"]] = cand

    rows = []
    unmatched = defaultdict(lambda: {"total": 0.0, "count": 0})
    all_seen_spenders = set()
    # Per-spender (not per-candidate/category) self-reported YTD tracking,
    # spans every contest a spender touches (e.g. AFP Action covers both
    # the Senate race and House races). Every row for a given group shows
    # this same figure, from the group's single most recent filing overall
    # -- even if that filing's own YTD line was about a different
    # candidate/support-oppose category than the row itself. Per Grant
    # 2026-07-21: a stale per-category number understates how current a
    # group's reporting actually is when they just haven't filed anything
    # new for THIS row's specific category lately.
    spender_self_report = defaultdict(lambda: {"date": "", "ytd": None})

    # RSS/FastFEC fast path: accelerate filings from spenders we've seen
    # before (persisted across cycles/restarts). Never blocks the main
    # aggregation on failure.
    known_spenders_path = os.path.join(output_dir, ".known_spenders.json")
    committee_names_path = os.path.join(output_dir, ".committee_names.json")
    se_cache_dir = os.path.join(output_dir, ".se_filing_cache")
    os.makedirs(se_cache_dir, exist_ok=True)

    known_spenders = set(load_json(known_spenders_path, []))
    committee_names = load_json(committee_names_path, {})
    rss_by_contest = defaultdict(list)

    if use_rss and known_spenders:
        print(f"  Checking RSS feed for {len(known_spenders)} known outside spender(s)...", end=" ")
        rss_filings = fetch_rss_filings(known_spenders)
        print(f"{len(rss_filings)} recent filing(s)")
        for committee_id, file_number, form_type in rss_filings:
            txns = parse_se_filing(file_number, form_type, se_cache_dir)
            for t in txns:
                cid = t["committee"]["committee_id"]
                t["committee"]["name"] = get_committee_name(cid, committee_names)
                rss_by_contest[se_contest_key(t)].append(t)

    contest_cache_dir = os.path.join(output_dir, ".se_contest_cache")
    os.makedirs(contest_cache_dir, exist_ok=True)

    for (state, office, district), slug_map in by_contest.items():
        label = f"{state} {office}{district or ''}"
        print(f"  {label:<10} pulling Schedule E for {len(slug_map)} tracked candidate(s)...", end=" ")
        cache_path = os.path.join(contest_cache_dir, f"{state}_{office}_{district or 'na'}.json")
        try:
            transactions = fetch_schedule_e_for_contest(state, office, district, cycle, min_date)
            save_json(cache_path, transactions)
        except Exception as e:
            # A failed pull (e.g. sustained API rate-limiting) must NOT be
            # treated as "this contest has zero outside spending" -- that
            # would silently wipe real numbers off the live sheet for a
            # full poll cycle. Fall back to this contest's last successful
            # pull instead, so a transient API failure degrades to stale
            # data, never to zero.
            cached = load_json(cache_path, None)
            if cached is not None:
                print(f"ERROR: {e} -- using last successful pull ({len(cached)} records, may be stale)")
                transactions = cached
            else:
                print(f"ERROR: {e} -- no prior successful pull to fall back on, skipping this cycle")
                continue
        rss_extra = rss_by_contest.get((state, office, district), [])
        if rss_extra:
            print(f"(+{len(rss_extra)} from RSS/FastFEC)", end=" ")
        transactions = dedupe_notice_vs_periodic(transactions + rss_extra)
        print(f"{len(transactions)} records")

        for t in transactions:
            cid = (t.get("committee") or {}).get("committee_id")
            if cid:
                all_seen_spenders.add(cid)

        # Group by (matched candidate, spending committee, support/oppose)
        def new_group():
            return {"period_totals": defaultdict(float), "cycle_total": 0.0,
                     "count": 0, "last_date": "", "name": "", "last_file_number": None}
        groups = defaultdict(new_group)
        for t in transactions:
            if t.get("memoed_subtotal"):
                continue
            last_slug = slugify(t.get("candidate_last_name"))
            cand = slug_map.get(last_slug)
            if cand is None:
                display_name = t.get("candidate_name") or t.get("candidate_last_name") or "UNKNOWN"
                unmatched[(label, display_name)]["total"] += float(t.get("expenditure_amount") or 0)
                unmatched[(label, display_name)]["count"] += 1
                continue
            committee = t.get("committee") or {}
            spender_id = committee.get("committee_id")
            key = (cand["committee_id"], spender_id, t.get("support_oppose_indicator"))
            g = groups[key]
            g["name"] = committee.get("name", committee.get("committee_id", "UNKNOWN"))
            amount = float(t.get("expenditure_amount") or 0)
            date = t.get("expenditure_date") or ""
            g["cycle_total"] += amount
            g["count"] += 1
            for period, (start, end) in PERIOD_BOUNDS.items():
                if date >= start and (end is None or date <= end):
                    g["period_totals"][period] += amount
            if date > g["last_date"]:
                g["last_date"] = date
                g["last_file_number"] = t.get("file_number")

            # The committee's own self-reported running YTD total -- a
            # per-line cumulative figure, not something to sum across rows.
            # Tracked per SPENDER, not per candidate/category: every row for
            # a given group shows the figure from that group's single most
            # recent filing overall, even if that filing's own YTD line was
            # about a different candidate/support-oppose category than the
            # row -- a stale per-category number understates how current a
            # group's reporting actually is when they just haven't filed
            # anything new for THIS row's specific category lately.
            # Within a tie on date, take the MAX YTD seen, not "whichever
            # came last": a single filing routinely has multiple line items
            # sharing the same expenditure_date, each with its own YTD
            # snapshot as of that specific line (they build cumulatively
            # down the schedule), so date alone doesn't identify the final
            # figure. Confirmed on Unite to Win/Stevens filing 1998963 on
            # 2026-07-21: two lines both dated 2026-07-16, YTD $2,087,047
            # and $2,787,047 -- picking "last by date" on a tie is
            # essentially arbitrary and grabbed the non-final one. Since
            # this counter never decreases, the max is always correct.
            # May be blank on some records (older filings, some report
            # types); that's fine, just leave it as-is if no better value.
            ytd = t.get("office_total_ytd")
            if ytd not in (None, "") and spender_id:
                ytd = float(ytd)
                sr = spender_self_report[spender_id]
                if date > sr["date"] or (date == sr["date"] and (sr["ytd"] is None or ytd > sr["ytd"])):
                    sr["date"] = date
                    sr["ytd"] = ytd

        for (committee_id, spender_id, support_oppose), g in groups.items():
            cand = candidates_by_committee[committee_id]
            row = {
                "Candidate Name": cand["candidate_name"],
                "First Name": cand.get("first_name", ""),
                "Contest ID": cand["contest_id"],
                "Party": cand.get("party", "OTH"),
                "Support/Oppose": "Support" if support_oppose == "S" else "Oppose",
                "Outside Group": g["name"],
                "SUM CandCategory": round(g["cycle_total"], 2),
                "# Transactions": g["count"],
                "Most Recent Expenditure": g["last_date"],
                "FEC Committee Link": f"https://www.fec.gov/data/committee/{spender_id}/",
                "FEC Most Recent Report Link": (
                    f"https://docquery.fec.gov/cgi-bin/forms/{spender_id}/{g['last_file_number']}/se"
                    if g["last_file_number"] else ""
                ),
                "_spender_id": spender_id,
            }
            for period in PERIOD_BOUNDS:
                row[period] = round(g["period_totals"].get(period, 0.0), 2)
            rows.append(row)

        time.sleep(0.2)

    if unmatched:
        print("  Untracked candidates with outside spending in these contests (not in candidates.csv):")
        for (label, name), agg in sorted(unmatched.items(), key=lambda kv: -kv[1]["total"])[:10]:
            print(f"    {label:<10} {name}: ${agg['total']:,.0f} ({agg['count']} txns)")

    known_spenders |= all_seen_spenders
    save_json(known_spenders_path, sorted(known_spenders))
    save_json(committee_names_path, committee_names)

    # SUM GroupAll: this outside group's total spend across every candidate
    # and race it's touched, not just the one this row is about -- lets a
    # reader see a group's full footprint without hunting across rows.
    # SELFREPORT YTD Spend: the group's self-reported YTD from its single
    # most recent filing overall, same reasoning -- see spender_self_report
    # above. Both computed after every contest is processed, since a group
    # (e.g. AFP Action) can spend across multiple House races plus the
    # Senate race, each handled in a separate contest-loop iteration above.
    spender_totals = defaultdict(float)
    for row in rows:
        spender_totals[row["_spender_id"]] += row["SUM CandCategory"]
    for row in rows:
        spender_id = row.pop("_spender_id")
        row["SUM GroupAll"] = round(spender_totals[spender_id], 2)
        ytd = spender_self_report.get(spender_id, {}).get("ytd")
        row["SELFREPORT YTD Spend"] = round(ytd, 2) if ytd is not None else ""

    rows.sort(key=lambda r: r["SUM CandCategory"], reverse=True)
    return rows


def write_csv(rows, output_path):
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ── Sheets upload ────────────────────────────────────────────────────────────

def upload_to_sheets(rows, sheet_id, credentials_path, worksheet_name):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

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
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=500, cols=20)

    data = [OUTPUT_COLUMNS] + [[r[c] for c in OUTPUT_COLUMNS] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    ws.format(f"A1:{chr(64 + len(OUTPUT_COLUMNS))}1", {"textFormat": {"bold": True}})
    ws.format(f"G2:M{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})

    # Pale background tints on the three "which number is this" columns,
    # so a reader can tell at a glance which figure they're looking at
    # without rereading the header every time.
    def col_letter(name):
        return chr(65 + OUTPUT_COLUMNS.index(name))

    tinted_columns = {
        "SUM CandCategory": {"red": 1.0, "green": 1.0, "blue": 0.8},    # pale yellow
        "SUM GroupAll": {"red": 0.85, "green": 1.0, "blue": 0.85},      # pale green
        "SELFREPORT YTD Spend": {"red": 0.85, "green": 0.92, "blue": 1.0},  # pale blue
    }
    for column_name, color in tinted_columns.items():
        letter = col_letter(column_name)
        ws.format(f"{letter}1:{letter}{len(rows) + 1}", {"backgroundColor": color})

    ws.set_basic_filter(f"A1:{chr(64 + len(OUTPUT_COLUMNS))}{len(rows) + 1}")
    ws.freeze(cols=1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Track outside-group (Schedule E) spending for candidates.")
    parser.add_argument("--candidates", default="candidates.csv")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cycle", required=True, type=int)
    parser.add_argument("--min-date", default="2025-01-01",
                         help="Earliest expenditure_date to include (YYYY-MM-DD)")
    parser.add_argument("--poll-interval", type=int, default=900)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-rss", action="store_true",
                         help="Disable the RSS/FastFEC fast path for known spenders "
                              "(falls back to api.open.fec.gov only)")
    parser.add_argument("--sheet-id", default="10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc")
    parser.add_argument("--worksheet", default="Outside Spending")
    parser.add_argument("--credentials", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..",
        "app-template-access-402821-3111eabfc82d.json"))
    args = parser.parse_args()

    if not os.environ.get("FEC_API_KEY"):
        sys.exit("Error: FEC_API_KEY environment variable not set.")

    output_csv = os.path.join(args.output_dir, "output", f"outside_spending_{args.cycle}.csv")
    os.makedirs(os.path.join(args.output_dir, "output"), exist_ok=True)

    while True:
        with open(args.candidates, newline="", encoding="utf-8") as f:
            candidates = list(csv.DictReader(f))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] Pulling Schedule E for outside spending ({len(candidates)} candidates)...")
        rows = aggregate(candidates, args.cycle, args.min_date, args.output_dir, use_rss=not args.no_rss)
        write_csv(rows, output_csv)
        print(f"  Compiled {len(rows)} candidate/group rows -> {output_csv}")

        if args.sheet_id and os.path.exists(args.credentials):
            try:
                upload_to_sheets(rows, args.sheet_id, args.credentials, args.worksheet)
                print("  Sheets updated.")
            except Exception as e:
                print(f"  Sheets upload failed: {e}")

            try:
                overallspend.update_overallspend_chart(args.output_dir, args.sheet_id, args.credentials)
                print("  overallspend_chart updated.")
            except Exception as e:
                print(f"  overallspend_chart update failed: {e}")

            try:
                groupspend.update_groupspend_chart(args.output_dir, args.sheet_id, args.credentials)
                print("  groupspend_chart updated.")
            except Exception as e:
                print(f"  groupspend_chart update failed: {e}")

            try:
                groupspend.update_all_groups_chart(args.output_dir, args.sheet_id, args.credentials)
                print("  groupspend_chart_ALL updated.")
            except Exception as e:
                print(f"  groupspend_chart_ALL update failed: {e}")

        if args.once:
            print("\nSingle pass complete.")
            break

        print(f"\nNext check in {args.poll_interval // 60}m {args.poll_interval % 60}s...")
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
