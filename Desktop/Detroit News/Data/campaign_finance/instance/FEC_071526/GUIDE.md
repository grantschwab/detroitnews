# FEC Filing Monitor — User Guide (Q2 2026)

## What This Does

Four independent background scripts, pushing to their own tab in the same Google Sheet:

1. **`monitor.py` (Q2)** — Michigan candidate committee filings (Q2 2026 mid-year reports) → `cands_Q2`. Auto-detects new filings same-day via the FEC's own Electronic Filing RSS feed (no manual paste needed — see below), downloads and parses each new/amended filing with FastFEC, compiles a summary CSV, pushes to Sheets, sends a macOS notification. Polls every 30 min.
2. **`outside_spending.py`** — Independent expenditures (FEC Schedule E) by outside groups (Super PACs, party committees, anyone) supporting or opposing the same candidates → `outside_spend_TEST_Q2`. Queried by *contest* (state/office/district), not by a specific candidate — some filers only fill in the free-text candidate name and leave the structured candidate ID blank, which a candidate_id-only query would silently miss. Also auto-accelerates known spenders via the RSS feed + FastFEC (see below) when api.open.fec.gov is lagging.
3. **`monitor_preprimary.py`** — Michigan candidates' 12-day pre-primary (`report_type=12P`) reports, ahead of the Aug 4, 2026 primary → `cands_preprimary`. Only quarterly filers must file a 12P (11 CFR 104.5 exempts monthly filers). Uses its own `raw_preprimary/` dir and `.monitor_preprimary_state.json` state file so a 12P file_number never collides with a candidate's Q2 file_number in the shared `raw/` tree.
4. **`monitor.py` (Q1 amendment recheck)** — same script as #1, but pointed at `--quarter Q1` with `--worksheet cands_Q1`, so late amendments to already-passed quarters keep showing up instead of the tab going stale. See "Past-Quarter Amendment Rechecking" below for why this needed its own instance.

All four read the same `candidates.csv`.

### Past-Quarter Amendment Rechecking

A committee can amend an older quarter's report at any time — the FEC doesn't restrict amendments to the current filing window. The **actively-tracked quarter is always self-covering**: `monitor.py --quarter Q2` re-queries the API every poll and takes the highest file_number for Q2's coverage window regardless of whether that's the original or a later amendment, so Q2 amendments are caught automatically as long as that process is running.

The gap is **past** quarters that stop being actively polled once you move on to the next one. `monitor.py`'s state (`.monitor_state.json`) and `raw/` tree track only the single most-recently-seen file_number per committee — once Q2 filings start landing, Q1's file_numbers get silently overwritten in state, so there's no way to tell "was this committee's Q1 report ever amended" from state alone.

The fix: `monitor.py` now takes optional `--raw-subdir` and `--state-file` args (default to `raw` / `.monitor_state.json`, so the actively-tracked quarter's invocation is unaffected). A **second instance** runs permanently with `--quarter Q1 --raw-subdir raw_q1 --state-file .monitor_state_q1.json --worksheet cands_Q1`, polling on a slower 30-minute cadence. This gives Q1 its own independent, permanent state/raw tree, so it can keep re-verifying Q1's coverage window (via the same `verify_filing_coverage()` ground-truth check as everything else) indefinitely without ever touching Q2's data. When Q3 becomes the actively-tracked quarter, repeat this pattern: keep Q1 (or fold it into Q2) recheck running under its own subdir/state-file, and start a new Q2-recheck instance the same way.

---

## File Structure

```
FEC_071526/
├── code/
│   ├── candidates.csv          ← Master candidate list (edit this)
│   ├── monitor.py               ← Committee filing monitor
│   ├── outside_spending.py      ← Outside-group (Schedule E) tracker
│   ├── supervise.sh             ← Crash-restart wrapper used to launch both scripts
│   ├── verify_candidates.py     ← Read-only FEC API sanity check (run before filing day)
│   └── preprocess.py            ← One-time script to rebuild candidates.csv from FEC Notify export
├── raw/                          ← FastFEC output for the actively-tracked quarter (Q2), organized by contest/party/candidate
│   └── {contest_id}/{party}/{candidate_name}/{file_number}/F3N.csv
├── raw_q1/                       ← FastFEC output for the Q1 amendment-recheck instance (separate tree, same layout as raw/)
├── raw_preprimary/                ← FastFEC output for 12P pre-primary filings (separate tree, same layout as raw/)
├── output/
│   ├── campaign_finance_2026_Q2.csv   ← Committee filing summary (auto-updated)
│   ├── campaign_finance_2026_Q1.csv   ← Q1 amendment-recheck summary (auto-updated)
│   └── outside_spending_2026.csv      ← Outside spending summary (auto-updated)
├── .monitor_state.json           ← Tracks which Q2 committee filings have been processed (don't edit); carried over from FEC_041526 so Q1 filings aren't re-flagged as new
├── .monitor_state_q1.json        ← Separate state file for the Q1 amendment-recheck instance (don't edit)
├── .monitor_preprimary_state.json ← Separate state file for the 12P pre-primary instance (don't edit)
├── .known_spenders.json          ← outside_spending.py: committee IDs of every outside group seen spending on a tracked race (don't edit; grows automatically)
├── .committee_names.json         ← outside_spending.py: cache of committee_id → name for RSS/FastFEC-sourced spenders (don't edit)
├── .se_filing_cache/              ← outside_spending.py: cached parsed Schedule E rows per filing_id, avoids re-parsing (safe to delete to force a re-pull)
└── app-template-access-402821-3111eabfc82d.json   ← Google service account credentials
```

---

## candidates.csv Columns

| Column | Description |
|---|---|
| `committee_id` | FEC committee ID (e.g. C00711317) |
| `committee_name` | Full committee name |
| `candidate_name` | Lowercase last name slug (e.g. `scholten`) |
| `first_name` | Lowercase first name (e.g. `hillary`) |
| `contest_id` | Race identifier: `mi00` = Senate, `mi03` = House district 3 |
| `party` | DEM, REP, IND, OTH, PAC |
| `force_id` | Filing number to use when FEC API hasn't indexed it yet (see below) |

---

## Before Filing Day: Verify the Candidate List

`candidates.csv` was carried over from the Q1 build (April 2026) and hasn't been rechecked against live FEC data since. Run this once before relying on it:

```bash
cd "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/code"
export FEC_API_KEY="qTG3dgVaQs5TjGq9Z2yQEH71weeD4pP3W1c0Jpnx"
python3 verify_candidates.py --candidates candidates.csv
```

It checks, for each row, whether `committee_id` is still the candidate's *current principal campaign committee* per the FEC API (candidate IDs and committee IDs are separate records, and a candidate can have multiple committees over time), and separately flags any active MI House/Senate 2026 candidates missing from the list entirely. It does **not** edit `candidates.csv` — several rows carry deliberate manual overrides (see Special Cases below), so review the report and hand-edit as needed. The "missing candidates" list will include many minor/no-activity filers since `candidates.csv` is a curated list, not exhaustive — only investigate names you recognize.

---

## Filing Day Workflow

### Terminal setup (do this once per terminal tab)
```bash
cd "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/code"
export FEC_API_KEY="qTG3dgVaQs5TjGq9Z2yQEH71weeD4pP3W1c0Jpnx"
```

### Unattended background run (recommended — survives closing the terminal tab, Mac won't sleep, auto-restarts on crash)

Both scripts run through `supervise.sh`, a small restart-loop wrapper: if either script crashes (uncaught exception), it sends a macOS notification and restarts it automatically. If it crashes repeatedly — 5 times within 30 minutes — the supervisor gives up, sends a final "needs help" notification, and stops retrying rather than looping forever against something genuinely broken. Check the log to see the actual error if that happens.

```bash
rm -f ../monitor_q2.stop ../outside_spending_q2.stop

nohup caffeinate -i ./supervise.sh monitor ../monitor_q2.stop 5 1800 \
  python3 -u monitor.py \
  --candidates candidates.csv --output-dir '..' \
  --cycle 2026 --quarter Q2 --worksheet "cands_Q2" \
  > ../monitor_q2.log 2>&1 &

nohup caffeinate -i ./supervise.sh outside_spending ../outside_spending_q2.stop 5 1800 \
  python3 -u outside_spending.py \
  --candidates candidates.csv --output-dir '..' \
  --cycle 2026 --worksheet "outside_spend_TEST_Q2" \
  > ../outside_spending_q2.log 2>&1 &
```

`-u` (unbuffered) matters — without it, Python buffers stdout when redirected to a file and `tail -f` shows nothing until a full poll cycle completes.

- Check on either process: `tail -f ../monitor_q2.log` or `tail -f ../outside_spending_q2.log`
- Confirm both are running (4 processes each — `caffeinate`, `bash supervise.sh`, and the `python3` child): `ps aux | grep -E "supervise.sh|monitor.py|outside_spending.py"`
- **Stop both at end of day** (do NOT just `pkill` the python processes alone — the supervisor will just restart them; touch the stop file first so the supervisor exits cleanly instead of respawning):
  ```bash
  touch ../monitor_q2.stop ../outside_spending_q2.stop
  pkill -f "supervise.sh|monitor.py|outside_spending.py"
  ```

### Manual single-pass trigger (e.g. to test a change)
```bash
python3 monitor.py --candidates candidates.csv --output-dir '..' --cycle 2026 --quarter Q2 --worksheet "cands_Q2" --once
```

---

## Auto-Detecting Filings (No Manual Paste Needed)

Both scripts use the **FEC Electronic Filing RSS feed** (`https://efilingapps.fec.gov/rss/generate?cids=...`) — the same real-time feed FEC Notify emails are built on — to close the gap when `api.open.fec.gov` is slow to index a filing (confirmed on 2026-07-15: a Schedule E notice sat unindexed by the API for at least a day; RSS + FastFEC caught it immediately). Each uses it differently:

### monitor.py (committee filings)

Every poll cycle it queries the feed for all of `candidates.csv`'s committee IDs in one request, and any filing found there seeds `force_id` automatically (see `fetch_rss_force_ids()`). No email-watching or manual pasting required.

Priority order per candidate, highest wins: `--force` CLI flag > manual `force_id` column in `candidates.csv` > RSS feed > api.open.fec.gov itself (which eventually takes over once it indexes the filing).

- **Manual `force_id` is now a fallback/override, not the primary workflow.** Still useful to force a specific filing number immediately, or if the RSS feed is ever down.
- **Disable**: pass `--no-rss` to `monitor.py`.
- The RSS feed only covers the **last 7 days**, and is filtered to periodic report form types (`F3N`, `F3X`, and amendments) — statements of organization, 24/48-hour IE notices, etc. are excluded so a stray filing doesn't get force-processed as a quarterly report.

### Wrong-quarter amendments (fixed 2026-07-15, two layers of protection)

A committee can amend an *older* report at any time — e.g. Stevens amending her Q3 2025 report on 2026-07-15. The RSS feed doesn't distinguish "new filing for the current quarter" from "amendment of something old," and the naive "take the most recent F3-family filing" logic will happily grab that old amendment and load it as if it were the current quarter's data. This actually happened in production: three candidates (Stevens, Ufford, McCann) briefly had wrong-quarter data loaded before being caught and reverted.

Two independent fixes now guard against this:
1. **`fetch_rss_force_ids()`** parses `CoverageFrom`/`CoverageThrough` out of the RSS item and only accepts a filing whose `CoverageThrough` matches the target quarter's end date (`QUARTER_END`).
2. **`verify_filing_coverage()`** is the real backstop: after *any* filing is downloaded via FastFEC — regardless of whether the file_number came from the API, RSS, a manually-pasted `force_id`, or a `--force` flag — it reads `coverage_through_date` directly out of the downloaded `F3N.csv`/`F3A.csv` and rejects (discards the download, leaves state untouched) if it doesn't match the target quarter. This is the ground-truth check that would have caught the incident even without fix #1.

If you ever manually paste a `force_id`, this backstop will silently reject it (with a `REJECTED: ... discarding` log line) if it turns out to be the wrong quarter — check the log if a manually-forced candidate never shows up as processed.

**Recovery runbook** if a wrong-quarter filing slips through anyway (shouldn't happen now, but if you spot bad data in `cands_Q2`):
```bash
# 1. Stop the loop first
touch ../monitor_q2.stop && pkill -f "supervise.sh monitor|monitor.py"

# 2. Delete the bad download
rm -rf ../raw/{contest_id}/{party}/{candidate_name}/{bad_file_number}

# 3. Clear the bad state entry (or reset to a known-good file_number if one exists)
python3 -c "
import json
d = json.load(open('../.monitor_state.json'))
for k in ('file_number', 'amendment_indicator', 'last_updated'):
    d['{committee_id}'].pop(k, None)
json.dump(d, open('../.monitor_state.json', 'w'), indent=2)
"

# 4. Recompile and push the corrected sheet
python3 monitor.py --candidates candidates.csv --output-dir '..' --cycle 2026 --quarter Q2 --worksheet "cands_Q2" --once

# 5. Relaunch the supervised loop (see Start command above)
```

To audit all currently-downloaded filings for coverage mismatches at any time:
```bash
python3 -c "
import csv, os, glob
raw_dir = '../raw'
target = '2026-06-30'
for path in glob.glob(os.path.join(raw_dir, '*/*/*/*/F3N.csv')) + glob.glob(os.path.join(raw_dir, '*/*/*/*/F3A.csv')):
    with open(path, newline='', encoding='utf-8') as f:
        row = next(csv.DictReader(f), None)
    if row and row.get('coverage_through_date') != target:
        print(path.split('/')[-3], path.split('/')[-2], row.get('coverage_through_date'))
"
```

### outside_spending.py (Schedule E)

RSS can't be queried by contest — only by known committee ID — so it accelerates spenders **already seen at least once** via the normal contest-based API query (persisted in `.known_spenders.json`, grows automatically). It does not speed up discovering a brand-new spender's very first filing; that's still bounded by the API contest query's own lag. In practice this covers the case that matters most: a known heavy spender (a Super PAC already active in a race) filing again before the API catches up.

Each cycle, for every known spender: query the RSS feed for Schedule-E-bearing filings (`F24`/`F5` notices, `F3X` periodic reports) from the last 7 days, and for any filing not already in `.se_filing_cache/`, download and parse it directly with FastFEC — bypassing `api.open.fec.gov` entirely for that filing. Those rows get merged into the same per-contest transaction list as the API results and pass through the same `dedupe_notice_vs_periodic()` dedup — see that function's docstring for the full two-pass logic (FEC `sub_id`-based cross-source matching, plus a notice-always-wins-over-periodic-restatement pass) — so once the API eventually indexes the same filing, the duplicate collapses to one entry rather than double-counting.

- **Disable**: pass `--no-rss` to `outside_spending.py`.
- **Non-fatal by design** for both scripts: an RSS fetch or FastFEC parse failure just logs a warning and falls through to the API-only path — never blocks a poll cycle.
- First run after `.known_spenders.json` doesn't exist yet (or is empty) skips the RSS step entirely — it bootstraps from the first cycle's API results, then accelerates from the second cycle on.

---

## Google Sheet

**URL:** https://docs.google.com/spreadsheets/d/10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc/edit

- **"cands_Q2" tab** — committee filings, written by `monitor.py`. Filter dropdowns on all columns, `Q Total_Receipts` filtered to > $5,000 by default, light yellow highlight on key dollar columns, `Q Burn Rate` as a percentage. Separate tab from Q1's data ("cands_Q1"), created automatically on first run.
- **"outside_spend_TEST_Q2" tab** — independent expenditures, written by `outside_spending.py`. One row per (candidate, outside group, support/oppose), sorted by cycle-to-date spend descending, with filter dropdowns and currency formatting on the dollar columns. Fully recomputed each poll cycle (not incremental) since Schedule E amendments and notice/periodic overlap need re-resolving every time — see the dedup logic in `dedupe_notice_vs_periodic()`.

If you rename a tab in the Sheets UI while a background loop is running, the loop won't know — on its next poll it'll recreate a fresh blank tab under the old name it still has in memory. Kill and relaunch with `--worksheet` pointed at the new name if you rename a tab mid-day.

**On timeliness for outside spending:** Super PACs and party committees file Schedule E on the same periodic (quarterly/monthly) cadence as candidate committees, except within 20 days of an election, when spends over $1,000–$10,000 trigger mandatory 24/48-hour reports. Since the general election is in November, tomorrow's outside-spending numbers will update on the same cadence and with similar API-indexing lag as the committee filings — not real-time, but not stale either.

---

## Output CSV Columns — campaign_finance_2026_Q2.csv (in order)

| Column | Description |
|---|---|
| Candidate Name | Lowercase last name slug |
| First Name | Lowercase first name |
| District | Two-digit district number (from contest_id, not FEC filing) |
| Party | DEM / REP / IND / OTH / PAC |
| Contest ID | e.g. mi04, mi00 |
| Q Receipts minus Loans | Q Total Receipts minus Q Self Loans |
| Q Total_contributions | Quarter total contributions |
| Q Total_Receipts | Quarter total receipts |
| Q Transfer_auth_committees | Transfers from authorized committees |
| Q Individual_Itemized | Itemized individual contributions |
| Q Cash on Hand | Cash on hand at end of quarter |
| Q Self Loans | Candidate self-loans this quarter |
| Q Expenditures | Quarter operating expenditures |
| Q Debt | Total debt |
| Q Burn Rate | Q Expenditures ÷ Q Total_Receipts (as %) |
| C Total_Receipts | Cycle-to-date total receipts |
| C Individual_Itemized | Cycle-to-date itemized individual contributions |
| C Expenditures | Cycle-to-date expenditures |
| C Self Loans | Cycle-to-date self-loans |
| Candidate Committee | Full committee name |
| FEC Link | Link to committee page on FEC.gov |
| Amendment | "Original" or "Amendment" |

## Output CSV Columns — outside_spending_2026.csv (in order)

| Column | Description |
|---|---|
| Candidate Name | Lowercase last name slug |
| First Name | Lowercase first name |
| Contest ID | e.g. mi04, mi00 |
| Party | DEM / REP / IND / OTH / PAC |
| Support/Oppose | "Support" or "Oppose" |
| Outside Group | Name of the spending committee (Super PAC, party committee, etc.) |
| 2025 Spent | Sum of `expenditure_amount`, 2025-01-01 through 2025-12-31 |
| Q1 2026 Spent | Sum of `expenditure_amount`, 2026-01-01 through 2026-03-31 |
| Q2 2026 Spent | Sum of `expenditure_amount`, 2026-04-01 through 2026-06-30 |
| Since Jul 1 Spent | Sum of `expenditure_amount`, 2026-07-01 onward (open-ended) |
| SUMMED Cycle Spend | Sum of `expenditure_amount` since 2025-01-01 (the `--min-date` default, the full 2025–2026 cycle) across every itemized Schedule E line we have — equals 2025 + Q1 + Q2 + Since Jul 1 |
| SELFREPORT YTD Spend | The committee's own self-reported running YTD total, straight from `office_total_ytd` (API) / `calendar_y_t_d_per_election_office` (FastFEC). This field is a per-line cumulative counter, not something to sum — we take the **maximum** value seen across the group's transactions (not "whichever came with the latest date": a single filing routinely has multiple line items sharing one expenditure_date, each carrying its own YTD snapshot as of that specific line, so date alone doesn't identify the final figure — confirmed on Unite to Win/Stevens filing 1998963 on 2026-07-21, two same-date lines with YTD $2,087,047 and $2,787,047; picking "last by date" on that tie grabbed the non-final one). Blank if no record in the group ever had a value. |
| # Transactions | Count of Schedule E line items summed, all periods combined |
| Most Recent Expenditure | Latest `expenditure_date` in the group |
| FEC Committee Link | `https://www.fec.gov/data/committee/{committee_id}/` for the outside group (the spender, not the candidate) |
| FEC Most Recent Report Link | `https://docquery.fec.gov/cgi-bin/forms/{committee_id}/{file_number}/se` for the filing behind the group's most recent expenditure — jumps straight to that filing's Schedule E page |

Period boundaries are hardcoded in `PERIOD_BOUNDS` in `outside_spending.py` — update them each quarter, or generalize if this becomes a running multi-quarter tool.

### SUMMED vs. SELF REPORT — why they can differ

`SUMMED Cycle Spend` is derived independently from FEC's published itemized Schedule E data (every line we can see, deduped). `SELFREPORT YTD Spend` is a number the committee computes and reports themselves on each filing — the two are **not cross-validated against each other by FEC**, so a gap between them isn't necessarily a bug in this pipeline. Confirmed on A Stronger Michigan/Stevens on 2026-07-21: SUMMED came to $13,482,482.77 from an exhaustive check of all 30 published itemized lines (no amendments, no duplicates, no missing records) — the SELF REPORT figure was $13,632,482.77, a $150,000 gap with no itemized line anywhere in FEC's data to account for it. That's either a bookkeeping error on the committee's end or unitemized spending baked into their running counter — worth asking their treasurer about rather than assuming a data pipeline issue when you see one of these gaps.

**But check for a real pipeline bug first if the gap is large.** Also on 2026-07-21, AFP Action/Rogers showed SUMMED $8.9M vs. SELF REPORT $4.8M — that one *was* a real bug: AFP is a monthly filer, and their periodic F3X reports routinely restate a transaction already disclosed via 24/48-hour notice, re-dated to somewhere else in that month's coverage period (up to 23 days off from the original notice date). `dedupe_notice_vs_periodic()`'s exact-date matching couldn't catch a gap that wide. Fixed by keying on FEC's own `sub_id` (a guaranteed-unique transaction identifier) for genuine duplicates, plus a second pass that drops any periodic (`is_notice=False`) record whose committee/candidate/support-oppose/payee/amount matches an existing notice (`is_notice=True`) record *regardless of date* — notices always win over periodic restatements. Verified against FEC's own "24/48 hour report" spend data explorer export for this committee: after the fix, SUMMED and SELF REPORT both landed on $4,774,152.50 with an exact 27-of-27 `sub_id` match. See `dedupe_notice_vs_periodic()`'s docstring for the full reasoning and the two failure modes it had to avoid (undercounting genuinely repeated same-amount payments to one vendor, and losing a real notice to a same-date periodic duplicate).

---

## Key Configuration

- **Sheet ID:** `10ILJsuZIXvsreJdHPpYZK_g4T4nEGXhMGVw8VtZXkgc`
- **Credentials:** `../app-template-access-402821-3111eabfc82d.json` (resolved relative to `code/`, so this instance folder is self-contained)
- **Poll interval:** 900 seconds (15 minutes) for both scripts
- **FEC API base:** `https://api.open.fec.gov/v1`

---

## Adding a New Candidate

Add a row to `candidates.csv` with:
- The committee ID (from FEC.gov or FEC Notify)
- A lowercase last name slug for `candidate_name`
- Lowercase first name for `first_name`
- The correct `contest_id` (check FEC API — it's sometimes wrong)
- Party abbreviation

Both scripts reload `candidates.csv` on every poll cycle, so no restart needed. `outside_spending.py` will resolve and cache the new candidate's `candidate_id` on its next cycle automatically.

---

## Special Cases in candidates.csv

| Committee | Issue | Fix Applied |
|---|---|---|
| C00437889 (Peters) | FEC API returns old House district mi14 | `CONTEST_OVERRIDES` in preprocess.py → mi00 |
| C00726042 (McClain) | FEC API returns wrong district | `CONTEST_OVERRIDES` → mi09 |
| C00864207 (McDonald Rivet) | Compound surname | `SLUG_OVERRIDES` → mcdonaldrivet |
| C00638650 (Stevens House) | Running for Senate now | In `EXCLUDE` list in preprocess.py |
| C00913269 (Honor, Duty & Discipline PAC) | PAC, not candidate committee | In `PAC_WHITELIST` in preprocess.py |

---

## Running preprocess.py (future cycles)

Use this when you have a new FEC Notify subscriptions export and need to rebuild candidates.csv from scratch. **Check output carefully — it may need manual corrections (see Special Cases above), and run `verify_candidates.py` afterward.**

```bash
cd "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/code"
export FEC_API_KEY="your_key"
python preprocess.py --subscriptions ../raw/subscriptions.csv --output candidates.csv
```

---

## Senate Primary Retrospective Side Project (2026-08-11)

Separate from the four continuously-running scripts above: a set of one-off, run-by-hand scripts covering the concluded MI Senate primary (Stevens, El-Sayed, McMorrow, Rogers), not wired into any polling loop.

- **`senate_retrospective.py`** — per-candidate campaign + outside-money totals (`SEN_retro_overall`, `SEN_retro_groups` tabs in the graphics spreadsheet, `1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY`), capped at 2026-08-04 (primary day) so Rogers'/El-Sayed's ongoing general-election spending doesn't leak into a "primary retrospective" framing.
- **`senate_ad_spend.py`** — full-cycle TV/Digital/Mail/Text/etc. breakdown of each campaign's own advertising spend. Output saved locally, not pushed to any sheet: `output/senate_ad_spend_items.csv` (itemized, ~6,800 rows) and `output/senate_ad_spend_summary.csv` (per-candidate category totals).

### Why `senate_ad_spend.py` doesn't use api.open.fec.gov's `schedules/schedule_b/` endpoint

Confirmed directly (2026-08-11): querying it with `min_date=2026-04-01` or later returns **zero** records for all four Senate committees, despite their filed reports clearly showing large April–July disbursements (e.g. El-Sayed's 12-day pre-primary report alone shows $2.48M in that window). The API has a real indexing gap for these committees' most recent months. Instead, the script pulls each candidate's own filed reports directly via `committee/{id}/filings/` (Q1–Q3/YE 2025, Q1/Q2/12P 2026), downloads and parses each one's itemized Schedule B (Line 17, Operating Expenditures) via FastFEC — same mechanism `outside_spending.py`'s RSS fast path already uses — and dedupes to the latest amendment per (report_type, coverage window).

### Senate Ad Spend Categorization Methodology

**The structured field doesn't work.** FEC's Schedule B has a `category_code` field — the same standardized 3-digit purpose-code system used on Schedule E (e.g. "004" = Advertising Expenses). Confirmed: it is **blank on every single row, for all four campaigns, across their entire filing history**. None of them populate it. Categorization here is necessarily based on the free-text `expenditure_purpose_descrip` field instead, via keyword matching — not a clean structured filter, and not standardized across campaigns (El-Sayed's filings say "TV Advertising"; Rogers' say "Media Placement"; the wording is entirely up to each campaign's own compliance software/vendor).

**Categories** (checked in this order, first match wins — see `CATEGORY_KEYWORDS`/`_categorize()` in `senate_ad_spend.py`):

| Category | Matches on | Notes |
|---|---|---|
| Text/SMS | "text messag", "sms", "texting service" | Added per Grant 2026-08-11 (originally missed "texting service" as a phrase — see Bug #1 below) |
| TV | "tv advertis", "television advertis", "broadcast advertis" | |
| Digital | "digital advertis", "digital media", "digital consult", "digital services", "digital market", "online advertis", "social media advertis" | |
| Mail | "direct mail", "mail consult", "list rental", "postage" | |
| Bundled Media (TV/digital/other, not separable) | word-presence check: "media" + ("buy"/"placement"/"production"), or "ad production"/"ad buy"/"ad placement"/"video production" | Deliberately its own bucket, not folded into TV or Digital — Stevens' and Rogers' filings use these broad umbrella terms without specifying a medium, so guessing the split would misrepresent the data |
| Printing | "printing" | Edge case, included per Grant 2026-08-11 — could be mail-adjacent (pieces mailed separately, postage counted under Mail) or non-mail (yard signs, door hangers); genuinely ambiguous, included as ad-related on the theory that campaign literature is a communication channel regardless of delivery method |
| Communications/Social Media Consulting | "communications consult", "social media consult" | Edge case, included per Grant 2026-08-11 — could be press/messaging strategy (not paid ads) or paid social management; can't distinguish from purpose text alone |
| Digital Fundraising/List Consulting | "fundraising", "list acquisition", "paid acquisition" | Edge case, included per Grant 2026-08-11 — this is spend to *raise* money (donor acquisition, email lists), a genuinely different function from *persuading voters*, even though it runs through similar digital vendors. Included per explicit instruction, but worth remembering this is categorically different in kind from the other buckets if using this breakdown in a story |
| Other/non-ad | (default) | Payroll, travel, polling, legal, compliance, rent, insurance, processing fees, general campaign/political strategy consulting, field consulting, research — none of this mentions a specific ad channel |

**Two bugs found and fixed (Grant caught both, 2026-08-11):**

1. **"Texting Services" (standalone) was missed entirely.** The original Text/SMS keyword list only checked for "text messag"/"sms", which doesn't match the word "texting." $49,422 across 26 rows (mostly McMorrow) was sitting in Other/non-ad. Fixed by adding "texting service" as a keyword.
2. **"MEDIA CONSULTING / PRODUCTION / PLACEMENT" ($501,198 across 2 rows) was missed** because the original Bundled Media check used literal phrase substrings ("media placement", "media production"). This purpose string reads "...production / placement" — same concept, different word order, so the literal substring never matched. Fixed by switching to a word-presence check (`"media" in p and any(k in p for k in ["buy","placement","production"])`) that doesn't depend on word adjacency/order.

**Known, un-fixable limitation:** many purpose descriptions bundle multiple services into a single dollar figure, e.g. `"FUNDRAISING CONSULTING / MEDIA PRODUCTION / MEDIA PLACEMENT / SMS MESSAGES"` ($237,024, one line item). There's no way to tell from the text how that one dollar amount splits across the four services named — the full amount gets counted under whichever category is checked first in priority order (Text/SMS, in that example, since "sms messages" matches). This means category totals should be read as "at least this much was probably X," not as a precise split. Not something to try to fix further without going to invoice-level detail from each vendor, which FEC disclosure doesn't provide.

**`Total ad-related`** (used for `SEN_retro_overall`'s "Campaign (ad-related spend)" column) = everything except Other/non-ad, i.e. the sum of every category in the table above except the last row.

---

## Dependencies

- Python 3
- `fastfec` (CLI, must be installed and on PATH) — used by both `monitor.py` (every committee filing) and `outside_spending.py` (only for RSS-accelerated Schedule E filings)
- `gspread` + `google-auth` (`pip install gspread google-auth`)
- `curl` (standard on macOS)
