# Schedule E / Outside-Spending Tracking (Independent Expenditures)

Built for `outside_spending.py` in `FEC_071526/` — tracks Super PAC / party-committee spending for/against a curated list of candidates, without needing to know group names in advance. Companion to `fec_rss_realtime_monitoring.md` (the RSS/lag-avoidance half) and `fec_filing_structure.md` (general form reference).

## Discovery: Query by CONTEST, Not by candidate_id

Many outside spenders leave the structured `candidate_id` field blank on their Schedule E submission and only populate the free-text `candidate_name`/`candidate_office_*` fields. A `schedule_e?candidate_id=X` query silently misses every one of these — confirmed real: "Fighting for Michigan PAC" spending on Abdul El-Sayed had a null `candidate_id` and was invisible to candidate_id-based queries entirely.

Query by contest instead:
```python
params = {
    "candidate_office_state": "MI",
    "candidate_office": "S",       # or "H"
    "cycle": 2026,
    "min_date": "2025-01-01",
    "most_recent": "true",
}
if office == "H":
    params["candidate_office_district"] = "03"  # zero-padded 2-digit
```
Then match results back to your tracked candidates by last-name slug (`candidate_last_name`, slugified the same way as your own candidate list — lowercase, alnum-only). This catches every spender in the race regardless of how well they filled out the structured fields, at the cost of one query per contest instead of per candidate (14 contests for MI vs 66+ candidates — fewer API calls too).

## Deduplication Is the Hard Part — Two Distinct Failure Modes

`most_recent=true` only resolves amendments *of the same filing* (via FEC's internal sub_id/back_reference chain). It does **not** dedupe a notice against the periodic report that later restates the same expenditure — those are two independently-filed records that both satisfy `most_recent=true`. Naively summing double-counts.

### Failure mode 1: exact-date ties (same-day corrections)
A single filing can have multiple line items sharing one `expenditure_date`, each with its own cumulative YTD snapshot (see below). An amendment correcting an earlier report can also land on the exact same date as what it supersedes.

### Failure mode 2: date-shifted restatements (the expensive one)
A monthly/quarterly periodic filer's F3X report routinely restates a transaction already disclosed via 24/48-hour notice, but **re-dated to somewhere else in that report's coverage period** — not necessarily near the original date. Confirmed on a real committee: a notice dated Feb 2, the periodic restatement of the identical $500,000/payee dated Feb 25 — 23 days apart. This blew past any reasonable fixed date-tolerance window (inflated one group's total by ~$4M before catching it). Exact-date matching cannot catch this; you need a second pass.

### The two-pass fix that actually worked

```python
def dedupe_notice_vs_periodic(transactions):
    def content_key(t):
        c = t.get("committee") or {}
        return (c.get("committee_id"), slugify(t.get("candidate_last_name")),
                t.get("support_oppose_indicator"), slugify(t.get("payee_name")),
                round(float(t.get("expenditure_amount") or 0), 2))

    # Pass 1: collapse cross-SOURCE duplicates using FEC's own sub_id
    # (guaranteed-unique; present on API records, absent on FastFEC-parsed
    # ones). Two different real sub_ids are ALWAYS two real transactions --
    # keep both no matter how identical their content looks. A single
    # filing can legitimately have two genuine $14,000 charges to the same
    # vendor on the same day; collapsing on content alone undercounts.
    with_sub_id = [t for t in transactions if t.get("sub_id")]
    without_sub_id = [t for t in transactions if not t.get("sub_id")]
    seen, pass1 = set(), []
    for t in with_sub_id:
        if t["sub_id"] not in seen:
            seen.add(t["sub_id"]); pass1.append(t)
    covered = {content_key(t) + (t["expenditure_date"], bool(t.get("is_notice"))) for t in pass1}
    for t in without_sub_id:  # only RSS/FastFEC-sourced records lack sub_id
        key = content_key(t) + (t["expenditure_date"], bool(t.get("is_notice")))
        if key not in covered:
            covered.add(key); pass1.append(t)

    # Pass 2: drop periodic (is_notice=False) restatements of an
    # already-counted notice, REGARDLESS of date. Notices always win.
    # Deliberately do not merge two is_notice=True records with each other
    # even on a full content match -- recurring identical-amount payments
    # to the same vendor (a media buy paid out on a schedule) are common
    # and real; only a notice/periodic pairing is evidence of duplication.
    notice_keys = {content_key(t) for t in pass1 if t.get("is_notice")}
    return [t for t in pass1 if t.get("is_notice") or content_key(t) not in notice_keys]
```

**Do not prefer `is_notice=False` on an exact-date tie either** — an earlier version of this did, and it caused a real bug: when an RSS-sourced periodic restatement happened to land on the *exact same date* as its notice, "prefer periodic" silently discarded the genuine notice record.

**Validation method**: FEC's own "24/48 hour report" spend data explorer (`docquery.fec.gov` UI, exportable as CSV) is close to ground truth for a specific committee/candidate pair. Diff your dedup output against it on `sub_id` — an exact set match both ways is the strongest confirmation you can get.

## Payee Name Normalization

`api.open.fec.gov` and FastFEC-parsed filings format the same payee differently — e.g. `"IN PURSUIT OF LLC"` vs `"IN PURSUIT OF, LLC"`. A plain `.strip().upper()` treats these as different keys and silently double-counts. Run payee names through a full slugify (strip *all* punctuation and whitespace, not just case) before using them in any dedup key.

## Self-Reported YTD Field (`office_total_ytd` / `calendar_y_t_d_per_election_office`)

Each Schedule E line carries the committee's own self-reported cumulative YTD spend "per election office" — useful for cross-checking your own summed total, but has real sharp edges:

- **It's a running counter, not a per-line amount** — never sum it across rows. Track the value from the group's most recent record instead.
- **"Most recent by date" is not well-defined on ties.** Multiple line items in one filing routinely share the same `expenditure_date`, each with its own YTD snapshot as of that specific line (they build cumulatively down the schedule). Picking "whichever came last in iteration order" on a same-date tie is arbitrary. Fix: track the **maximum** YTD value seen, not "last by date" — the counter never decreases, so max is always the correct final figure regardless of tie-breaking.
- **It's scoped per-office, and a group's most recent filing might not be about the category you're displaying.** If you're showing "self-reported YTD" next to a candidate/support-or-oppose row, and the group's *last* filing happened to be about a different candidate, a naive per-category tracker shows a stale number even though the group has filed more recently overall. If the point of the column is "how current is this group's reporting," track it per-*spender* (across every candidate/category they've touched) and take the freshest one group-wide, not per-category.
- **A gap between your summed total and the self-reported total isn't automatically a bug.** FEC does not cross-validate the two. Confirmed real case: a committee's self-report exceeded an exhaustively-verified itemized sum by exactly $150,000 with no itemized line anywhere to account for it — a filer bookkeeping error, not a pipeline issue. Always rule out an actual dedup/completeness bug first (see above) before concluding it's just filer inconsistency — in one case what looked like committee sloppiness turned out to be our own dedup bug, and in another the reverse was true.

## FastFEC Parsing Detail for Schedule E

`SE.csv` fields worth knowing beyond the obvious (`payee_organization_name`, `expenditure_amount`, `support_oppose_code`, `candidate_last_name`):
- `disbursement_date` / `dissemination_date` — prefer `disbursement_date`, fall back to `dissemination_date` **only for period-bucketing purposes** (which quarter a dollar amount counts toward) — never for a "most recent"/currency display. See below.
- `date_signed` — the filer's per-line certification date. Always present, always `<=` today. Use this (not `expenditure_date`) for any "how current is this data" display. FEC API equivalent: `independent_sign_date`.
- `calendar_y_t_d_per_election_office` — the self-report YTD field, FastFEC's name for `office_total_ytd`
- `memo_code == "X"` — skip, informational/duplicate subtotal line
- `candidate_office` / `candidate_office_state` / `candidate_office_district` — present even when `candidate_id` is null, which is exactly what makes contest-based matching (see above) possible for FastFEC-sourced records too

FastFEC does **not** carry FEC's own `sub_id` — only `api.open.fec.gov` records have it. This matters for the dedup design above (pass 1 can only use `sub_id` on API-sourced records; RSS/FastFEC-sourced records fall back to content+date+is_notice matching).

## `expenditure_date` Can Silently Be a Future Dissemination Date, Not a Payment Date

**This applies to `api.open.fec.gov` too, not just FastFEC** — it's not a parsing quirk, it's how FEC itself populates the field. Confirmed directly: `GET schedules/schedule_e/?committee_id=...` returned `disbursement_dt: null, dissemination_date: "2026-08-04", expenditure_date: "2026-08-04"` on the same record — the API's own `expenditure_date` equals the (future) dissemination date whenever no true disbursement date has been filed. FastFEC's `SE.csv` mirrors this via `disbursement_date` (blank) / `dissemination_date` (populated).

This is routine on 24/48-hour independent-expenditure notices for pre-scheduled digital ad buys — the filer discloses a communication that will run days or weeks out, and hasn't yet reported (or doesn't yet know) the actual payment date. The dissemination date is a real, legitimate, forward-looking field on the form; the bug is treating it as interchangeable with "when did the spending happen" for a currency/freshness display.

**Real incident**: a "Most Recent Expenditure" column showed dates past the actual current date, confusing about which filing was actually most recent. Root cause traced to a committee's real filing (verified live via `docquery.fec.gov/cgi-bin/forms/{committee_id}/{file_number}/se`) with dissemination dates up to 12 days out.

**Fix**: use `independent_sign_date` (API) / `date_signed` (FastFEC) for any "most recent"/currency-of-data logic instead — the filer's per-line certification date, always present, and by definition can never be in the future (you can't sign a certification for something that hasn't happened yet). Keep `expenditure_date`'s existing disbursement-or-dissemination fallback for period-bucketing (which quarter a dollar amount counts toward) and dedup matching — an estimated event date is still the more meaningful anchor for "which period did this spending happen in" than the paperwork-filing date, so don't switch those to sign date too.

## Rate-Limit Failures Must Degrade to Stale, Never to Zero

Since this pipeline queries per-contest and fully rebuilds output each cycle, a rate-limited (`HTTP 429`) contest fetch that isn't specifically handled will silently produce zero rows for that contest -- wiping real dollar totals off the live sheet for a full cycle, not just failing loudly. Real incident: a sustained 429 storm (likely from restarting multiple background monitors simultaneously, all hitting the API at once) blanked the Senate race's outside-spending numbers. Fix: cache each contest's last successful raw pull to disk, fall back to it on failure. Full pattern and code in `google_sheets_flourish_pipeline.md`.

## `file_number` Is Not a Real Filter on `schedules/schedule_e/`

Confirmed directly (2026-09-09): querying `schedules/schedule_e/?file_number=X&committee_id=Y` with a **completely bogus, nonexistent** `file_number` returns the exact same result count as querying with a real one (2,567 records for America PAC either way — the committee's full unfiltered history). The API silently ignores the param rather than erroring or filtering.

This broke a real feature: an earlier `filing_is_superseded(file_number, committee_id)` check (built to catch an RSS/FastFEC-cached filing that was later amended away, so a stale cached copy wouldn't keep double-counting forever) used this param assuming it scoped results to one filing. In practice it was asking "has this committee *ever* had any amended filing" against an unstably-ordered ~100-record slice of its entire history — true for nearly every active committee, and non-deterministic across identical calls (confirmed: the same `(file_number, committee_id)` call returned both `True` and `False` on repeated invocations with no state change in between). This silently discarded valid, brand-new, never-amended RSS-sourced filings on essentially a coin flip, for any committee with amendment history — real incident: America PAC's 2026-09-08 filing (`2011127`, pushing its MI Senate total to $1,047,893.72) never made it into the live sheet because of this.

**Fix applied**: removed the check entirely rather than trying to repair it — there's no reliable way to scope a single filing's amendment status via this endpoint. This reintroduces the original, narrower risk it was built for (an RSS-cached filing amended *after* caching could double-count) as a better trade than routine, silent data loss. If that narrower case needs solving again, don't reach for `file_number` — pull the specific filing via `docquery.fec.gov/dcdev/posted/{file_number}.fec` directly (bypasses the search index/filter entirely, same technique the RSS/FastFEC fast path already uses) and inspect its own header for amendment info instead of relying on the API's filtering.

## Human-Readable Filing Links

Two different docquery.fec.gov URL patterns, useful for different things:
- `https://www.fec.gov/data/committee/{committee_id}/` — committee overview page
- `https://docquery.fec.gov/cgi-bin/forms/{committee_id}/{file_number}/se` — jumps straight to a specific filing's Schedule E page (swap `/se` for the relevant schedule letter for other schedules)
- `https://docquery.fec.gov/cgi-bin/fecimg/?{image_number}` — the raw filed-image viewer (needs the `image_number` field, not `file_number`)
