"""
groupspend.py

Per-committee breakdown for the El-Sayed vs. Rogers general election,
feeding two Flourish graphics alongside overallspend_chart's summary view:

- SEN_groups_chart_100k+: outside groups only, $100k+ total spend only,
  for a bar chart.
- SEN_groups_chart_ALL: every outside group (any spend amount) plus both
  campaigns, for a reference table.

Retargeted 2026-08-11 (Grant) from the primary matchup (Stevens vs.
El-Sayed) to the actual general-election matchup, now that the primary's
over and outside_spending_2026.csv (which this reads) no longer contains
any Stevens rows at all -- these tabs were otherwise permanently showing
$0 for one side.

Same pattern as overallspend.py: reads only the already-compiled output
CSVs written by monitor.py (campaign_finance_2026_Q2.csv) and
outside_spending.py (outside_spending_2026.csv) -- no new API calls.
Called at the end of outside_spending.py's own sheet-upload step.
"""

import csv
import os

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

OUTPUT_COLUMNS = ["Group", "Pro-Abdul", "Anti-Rogers", "Pro-Rogers", "Anti-Abdul"]
ALL_OUTPUT_COLUMNS = ["Group", "Supports", "Pro-candidate", "Anti-opponent", "Total"]

# Graphics tabs live in a separate spreadsheet from the main tracking
# sheet (candidate filings, raw outside-spending data) -- Flourish-facing
# only. Ignores whatever --sheet-id the caller passes for its own tab.
GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"

# Maps (candidate slug, Support/Oppose) -> which of the four spend columns it feeds
CATEGORY_COLUMN = {
    ("elsayed", "Support"): "Pro-Abdul",
    ("rogers", "Oppose"):   "Anti-Rogers",
    ("rogers", "Support"):  "Pro-Rogers",
    ("elsayed", "Oppose"):  "Anti-Abdul",
}

GROUP_COLUMNS = ["Pro-Abdul", "Anti-Rogers", "Pro-Rogers", "Anti-Abdul"]
VALUE_COLUMNS = GROUP_COLUMNS

# FEC committee names are filed in ALL CAPS. Known acronyms stay uppercase;
# articles/prepositions lowercase except as the first word; everything else
# gets normal Title Case.
ACRONYMS = {"PAC", "PAF", "UDP", "AP", "DMFI", "JDCA", "LLC", "GOP", "DNC",
            "RNC", "AIPAC", "NRA", "UAW", "SF", "SEIU", "COPE",
            "SLF", "GLCF", "AFSCME", "LCV", "EDF"}
LOWERCASE_WORDS = {"a", "an", "the", "of", "for", "to", "in", "and", "or",
                    "on", "at", "by", "from", "with"}

# Stylized brand names that don't follow normal Title Case (e.g. internal
# capitals). Checked case-insensitively against each raw word before the
# generic formatting rules below.
BRAND_OVERRIDES = {"MOVEON.ORG": "MoveOn.org", "VOTEVETS": "VoteVets", "WINSENATE": "WinSenate"}

# Well-known acronyms spelled out in-place as "Full Name (ABBREV)" --
# confirmed with Grant 2026-09-. Checked before the plain ACRONYMS
# uppercase-only handling below, so e.g. "SLF PAC" -> "Senate Leadership
# Fund (SLF) PAC" (only the acronym word itself is replaced; the rest of
# the committee's registered name is left as-is).
ACRONYM_EXPANSIONS = {
    "SLF": "Senate Leadership Fund (SLF)",
    "GLCF": "Great Lakes Conservative Fund (GLCF)",
    "AFSCME": "American Federation of State, County and Municipal Employees (AFSCME)",
    "LCV": "League of Conservation Voters (LCV)",
    "EDF": "Environmental Defense Fund (EDF)",
}

# Whole-name overrides for committees whose filed name needs restructuring,
# not just per-word acronym substitution -- e.g. the filed name puts "(AFP
# ACTION)" as the abbreviation of the whole preceding phrase, but Grant
# wants "AFP" read as short for just "Americans for Prosperity", with
# "Action, Inc." after it, and the DBA aliases (CVA Action, Libre Action)
# dropped as chart-label clutter. Checked against the raw, unformatted
# name (case-insensitive) before the word-by-word logic below runs at all.
FULL_NAME_OVERRIDES = {
    "AMERICANS FOR PROSPERITY ACTION, INC. (AFP ACTION) DBA CVA ACTION AND DBA LIBRE ACTION":
        "Americans for Prosperity (AFP) Action, Inc.",
}


def format_group_name(name):
    if name.strip().upper() in FULL_NAME_OVERRIDES:
        return FULL_NAME_OVERRIDES[name.strip().upper()]
    words = name.split(" ")
    out = []
    for i, word in enumerate(words):
        prefix, core, suffix = "", word, ""
        while core and not core[0].isalnum():
            prefix += core[0]
            core = core[1:]
        while core and not core[-1].isalnum():
            suffix = core[-1] + suffix
            core = core[:-1]
        if not core:
            out.append(word)
            continue
        if core.upper() in BRAND_OVERRIDES:
            formatted = BRAND_OVERRIDES[core.upper()]
        elif core.upper() in ACRONYM_EXPANSIONS:
            formatted = ACRONYM_EXPANSIONS[core.upper()]
        elif core.upper() in ACRONYMS:
            formatted = core.upper()
        elif i != 0 and core.lower() in LOWERCASE_WORDS:
            formatted = core.lower()
        elif "." in core:
            parts = core.split(".")
            formatted = parts[0].capitalize() + "." + ".".join(p.lower() for p in parts[1:])
        elif "-" in core:
            # e.g. "PRO-CHOICE" -> "Pro-Choice", not "Pro-choice"
            # (plain .capitalize() only capitalizes the very first letter).
            formatted = "-".join(p.capitalize() for p in core.split("-"))
        else:
            formatted = core.capitalize()
        out.append(prefix + formatted + suffix)
    return " ".join(out)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _group_rows(output_dir):
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    groups = {}
    if not os.path.exists(path):
        return groups
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("Candidate Name", "").strip().lower(),
                   row.get("Support/Oppose", "").strip())
            column = CATEGORY_COLUMN.get(key)
            if column is None:
                continue
            group = row.get("Outside Group", "").strip()
            if group not in groups:
                groups[group] = {c: 0.0 for c in VALUE_COLUMNS}
            groups[group][column] += _to_float(row.get("SUM CandCategory"))
    return groups


def _campaign_values(output_dir):
    """Cycle-to-date campaign expenditures from the Q2 quarterly filing --
    same source general_election.py uses for its own campaign-spend
    figures, so the two Senate-facing tab sets share one "as of" date."""
    path = os.path.join(output_dir, "output", "campaign_finance_2026_Q2.csv")
    labels = {"elsayed": "El-Sayed campaign", "rogers": "Rogers campaign"}
    result = {"El-Sayed campaign": 0.0, "Rogers campaign": 0.0}
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("Candidate Name", "").strip().lower()
            if name in labels:
                result[labels[name]] = _to_float(row.get("C Expenditures"))
    return result


MIN_TOTAL = 100000
MIN_TOTAL_1M = 1000000

# Raw (unformatted) "Outside Group" value for United Democracy Project, as
# filed -- used to match rows for the UDP-only primary-era breakout below.
UDP_RAW_NAME = "UNITED DEMOCRACY PROJECT ('UDP')"


def _udp_anti_abdul_primary(output_dir):
    """UDP's Anti-Abdul spend through the Aug 4 2026 primary only (SUM
    CandCategory minus Since Aug 5 Spent -- there's no dedicated
    "primary" period column in outside_spending_2026.csv, but everything
    NOT in the post-Aug-5 bucket is by construction from on-or-before the
    primary). One-off, UDP-specific column Grant asked for on
    SEN_groups_chart_1M+ only -- not a general per-group primary
    breakdown, so this isn't folded into _group_rows()."""
    path = os.path.join(output_dir, "output", "outside_spending_2026.csv")
    total = 0.0
    if not os.path.exists(path):
        return total
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("Outside Group", "").strip().upper() != UDP_RAW_NAME):
                continue
            if row.get("Candidate Name", "").strip().lower() != "elsayed":
                continue
            if row.get("Support/Oppose", "").strip() != "Oppose":
                continue
            total += _to_float(row.get("SUM CandCategory")) - _to_float(row.get("Since Aug 5 Spent"))
    return total


def _lean(values):
    """Which candidate a group's spending predominantly helps.
    Pro-Abdul + Anti-Rogers both help El-Sayed; Pro-Rogers + Anti-Abdul
    both help Rogers."""
    abdul_side = values.get("Pro-Abdul", 0.0) + values.get("Anti-Rogers", 0.0)
    rogers_side = values.get("Pro-Rogers", 0.0) + values.get("Anti-Abdul", 0.0)
    return "El-Sayed" if abdul_side >= rogers_side else "Rogers"


def build_rows(output_dir, min_total=MIN_TOTAL, max_rows=None):
    group_rows = _group_rows(output_dir)

    rows = []
    for group, values in group_rows.items():
        rows.append({"Group": format_group_name(group), **values,
                     "Total": sum(values[c] for c in VALUE_COLUMNS)})

    rows = [r for r in rows if r["Total"] >= min_total]
    rows.sort(key=lambda r: r["Total"], reverse=True)
    if max_rows is not None:
        rows = rows[:max_rows]
    return rows


def build_all_rows(output_dir):
    """Every outside group (any spend amount) plus both campaigns -- for
    the unfiltered reference table, not the $100k+ chart. Pro-Abdul/
    Anti-Rogers collapse into "Pro-candidate"/"Anti-opponent" (and
    Pro-Rogers/Anti-Abdul the same way), with "Supports" saying which
    candidate they refer to -- two fewer columns than showing all four
    directions separately."""
    group_rows = _group_rows(output_dir)
    campaign_values = _campaign_values(output_dir)

    rows = []
    campaign_supports = {"El-Sayed campaign": "El-Sayed", "Rogers campaign": "Rogers"}
    for label, spend in campaign_values.items():
        rows.append({"Group": label, "Supports": campaign_supports[label],
                     "Pro-candidate": 0.0, "Anti-opponent": 0.0, "Total": spend})
    for group, values in group_rows.items():
        supports = _lean(values)
        if supports == "El-Sayed":
            pro, anti = values["Pro-Abdul"], values["Anti-Rogers"]
        else:
            pro, anti = values["Pro-Rogers"], values["Anti-Abdul"]
        # Total sums all four raw categories, not just the dominant-side
        # pair shown -- protects against undercounting if a group ever
        # spends on both candidates at once (not seen in practice, but
        # Pro-candidate/Anti-opponent alone wouldn't capture it).
        total = sum(values[c] for c in GROUP_COLUMNS)
        rows.append({"Group": format_group_name(group), "Supports": supports,
                     "Pro-candidate": pro, "Anti-opponent": anti, "Total": total})

    rows.sort(key=lambda r: r["Total"], reverse=True)
    return rows


def build_backer_rows(output_dir, pro_column, anti_column, exclude_raw_names=()):
    """One row per outside group that spent on this candidate's side at
    all (Pro + Anti > 0) -- no minimum threshold, unlike build_rows()'s
    $100k/$1M floors, since these tabs are meant as a complete backer
    reference list, not a chart-sized top-N. No campaign-committee row
    (confirmed with Grant -- "backers" means outside groups only).

    exclude_raw_names: raw (unformatted) "Outside Group" values to leave
    out entirely -- e.g. UDP on the Rogers tab, per Grant (2026-09-22):
    almost all of UDP's Anti-Abdul spend is primary-era, already broken
    out separately on SEN_groups_chart_1M+, and Grant is adding his own
    explanatory note about it rather than showing it as a Rogers backer
    here."""
    exclude = {n.strip().upper() for n in exclude_raw_names}
    group_rows = _group_rows(output_dir)
    rows = []
    for group, values in group_rows.items():
        if group.strip().upper() in exclude:
            continue
        pro = values.get(pro_column, 0.0)
        anti = values.get(anti_column, 0.0)
        total = pro + anti
        if total <= 0:
            continue
        rows.append({"Group": format_group_name(group), pro_column: pro, anti_column: anti, "Total": total})
    rows.sort(key=lambda r: -r["Total"])
    return rows


def _read_existing_notes(spreadsheet, worksheet_name, group_column="Group", note_column="Note"):
    """Group -> Note, read from whatever's currently live in the sheet --
    used so a live-refreshing backer tab never overwrites Grant's
    hand-typed editorial notes. Returns {} if the tab doesn't exist yet
    (first run) or doesn't have the expected columns."""
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return {}
    values = ws.get_all_values()
    if not values:
        return {}
    header = values[0]
    if group_column not in header or note_column not in header:
        return {}
    g_idx, n_idx = header.index(group_column), header.index(note_column)
    notes = {}
    for row in values[1:]:
        if len(row) > max(g_idx, n_idx) and row[g_idx]:
            notes[row[g_idx]] = row[n_idx]
    return notes


def update_backer_chart(output_dir, credentials_path, worksheet_name, pro_column, anti_column, initial_notes,
                         exclude_raw_names=()):
    """Shared implementation for the two per-candidate "backers" tabs.
    Reads the sheet's CURRENT Note column first and carries each group's
    existing value forward into the freshly-computed row before doing
    the normal clear+rewrite -- so the note is never actually lost, just
    re-included in the same full-sheet write every cycle. A brand-new
    group (never seen in this tab before) gets seeded from
    initial_notes; an existing group's note (even one Grant deliberately
    left blank) is never replaced by initial_notes again."""
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")

    rows = build_backer_rows(output_dir, pro_column, anti_column, exclude_raw_names=exclude_raw_names)
    columns = ["Group", pro_column, anti_column, "Total", "Note"]

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_service_account_file(credentials_path, scopes=scopes)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(GRAPHICS_SHEET_ID)

    existing_notes = _read_existing_notes(spreadsheet, worksheet_name)
    for row in rows:
        row["Note"] = existing_notes.get(row["Group"], initial_notes.get(row["Group"], ""))

    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=max(len(rows) + 10, 20), cols=10)

    data = [columns] + [[r[c] for c in columns] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    ws.format("A1:E1", {"textFormat": {"bold": True}})
    # Only B:D (Pro/Anti/Total) get currency formatting -- E is the
    # free-text Note column, must not be reformatted as a number.
    ws.format(f"B2:D{len(rows) + 1}", {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})
    ws.freeze(cols=1)
    return rows


# First-try editorial notes (Grant asked for a draft to edit, not final
# copy) -- keyed by the formatted display name build_backer_rows()
# produces, drafted 2026-09-18 against the real current group list on
# each side. Cautious/generic where the group's specific identity isn't
# well-established from general knowledge -- Grant should verify/replace
# rather than trust these at face value.
INITIAL_NOTES_ROGERS = {
    "United Democracy Project ('UDP')": "AIPAC-aligned pro-Israel group",
    "Senate Leadership Fund (SLF) PAC": "GOP Senate leadership-aligned group",
    "Americans for Prosperity (AFP) Action, Inc.": "Koch network conservative group",
    "Great Lakes Conservative Fund (GLCF), Inc.": "Michigan-focused conservative group",
    "No Going Back PAC Inc.": "Conservative-aligned outside group",
    "The Sentinel Action Fund": "Conservative group",
    "America PAC": "Musk-founded pro-Trump group",
    "First Principles Digital": "Conservative digital ad group",
    "American Principles Project PAC": "Socially conservative advocacy group",
    "American Political Action Committee": "Conservative-aligned outside group",
    "A Stronger Michigan": "Michigan conservative-aligned group",
    "The Front Line Action": "Conservative-aligned outside group",
    "Red Senate": "GOP Senate-aligned outside group",
    "The Conservative Caucus Dba Americans for Constitutional Liberty": "Conservative advocacy group",
    "High Plains PAC": "Conservative-aligned outside group",
    "Associated Builders and Contractors, Inc. Political Action Committee (Abc PAC)": "Construction industry trade association's group",
    "NRA Victory Fund, Inc.": "NRA's political group",
}

INITIAL_NOTES_ELSAYED = {
    "WinSenate": "Democratic-aligned Senate outside group",
    "Defend the Vote": "Voting-rights-focused outside group",
    "Fighting for Michigan PAC": "Michigan Democratic-aligned group",
    "Environmental Defense Fund (EDF) Action Votes": "Environmental Defense Fund's political arm",
    "League of Conservation Voters (LCV) Victory Fund": "Environmental advocacy group",
    "Planned Parenthood Votes": "Reproductive-rights advocacy group",
    "Giffords PAC": "Gun-safety group founded by Gabby Giffords",
    "American Federation of State, County and Municipal Employees (AFSCME) Working Families Fund": "Public employees' union group",
    "PAF": "Progressive-aligned outside group",
    "National Nurses United for Patient Protection": "Nurses' union group",
    "American Priorities (AP)": "Progressive-aligned outside group",
    "Common Defense Action Fund": "Progressive veterans' advocacy group",
    "Cffe PAC": "Progressive-aligned outside group",
    "Working Families Party PAC": "Working Families Party-aligned group",
    "MoveOn.org Political Action": "Progressive advocacy group",
    "Millions of Michiganians": "Michigan progressive-aligned group",
    "For Michigan Action Fund": "Michigan progressive-aligned group",
    "Citizens Against AIPAC Corruption": "Anti-AIPAC advocacy group",
    "Unity & Justice Fund": "Progressive-aligned outside group",
    "Workers Vote": "Labor-aligned outside group",
    "Field Team 6, Inc.": "Progressive digital organizing group",
    "Forward Blue": "Democratic-aligned law enforcement group",
    "SF Solidarity PAC": "Progressive-aligned outside group",
    "Emgage Federal Political Action Committee": "Muslim American advocacy group",
    "Community Change Voters": "Progressive racial-justice advocacy group",
    "Indivisible Action": "Grassroots progressive movement's outside group",
    "Michigan Democratic State Central Committee": "Michigan Democratic Party committee",
    "International Alliance of Theatrical Stage Employees Federal Speech PAC": "Entertainment-industry stagehands' union group",
    "End the Occupation": "Palestine-solidarity-focused group",
    "The People United PAC": "Progressive-aligned outside group",
    "Progressive Turnout Project": "Progressive voter-turnout group",
    "Activate America": "Progressive-aligned outside group",
    "Givegreen United Action": "Environmental advocacy group",
}


def update_rogers_backers_chart(output_dir, credentials_path, worksheet_name="SEN_backers_Rogers"):
    # UDP excluded per Grant (2026-09-22) -- see build_backer_rows()'s
    # exclude_raw_names docstring.
    return update_backer_chart(output_dir, credentials_path, worksheet_name,
                                "Pro-Rogers", "Anti-Abdul", INITIAL_NOTES_ROGERS,
                                exclude_raw_names=(UDP_RAW_NAME,))


def update_elsayed_backers_chart(output_dir, credentials_path, worksheet_name="SEN_backers_Abdul"):
    return update_backer_chart(output_dir, credentials_path, worksheet_name,
                                "Pro-Abdul", "Anti-Rogers", INITIAL_NOTES_ELSAYED)


def _write_sheet(rows, columns, sheet_id, credentials_path, worksheet_name, blank_zeros=False):
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
        ws = spreadsheet.add_worksheet(title=worksheet_name, rows=max(len(rows) + 10, 20), cols=10)

    def cell(r, c):
        v = r[c]
        if blank_zeros and isinstance(v, (int, float)) and v == 0:
            return ""
        return v

    data = [columns] + [[cell(r, c) for c in columns] for r in rows]
    ws.clear()
    ws.update(values=data, range_name="A1")
    ws.format(f"A1:{chr(64 + len(columns))}1", {"textFormat": {"bold": True}})
    ws.format(f"B2:{chr(64 + len(columns))}{len(rows) + 1}",
              {"numberFormat": {"type": "CURRENCY", "pattern": "#,##0"}})
    ws.freeze(cols=1)


def update_groupspend_chart(output_dir, sheet_id, credentials_path, worksheet_name="SEN_groups_chart_100k+"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")
    rows = build_rows(output_dir, min_total=MIN_TOTAL)
    _write_sheet(rows, OUTPUT_COLUMNS, GRAPHICS_SHEET_ID, credentials_path, worksheet_name)
    return rows


OUTPUT_COLUMNS_1M = OUTPUT_COLUMNS + ["Anti-Abdul (primary only)", "Total"]


def update_groupspend_chart_1m(output_dir, sheet_id, credentials_path, worksheet_name="SEN_groups_chart_1M+"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")
    rows = build_rows(output_dir, min_total=MIN_TOTAL_1M, max_rows=10)
    udp_primary = _udp_anti_abdul_primary(output_dir)
    udp_display_name = format_group_name(UDP_RAW_NAME)
    for row in rows:
        if row["Group"] == udp_display_name:
            row["Anti-Abdul (primary only)"] = udp_primary
            # UDP's whole-cycle Anti-Abdul spend is now shown in the
            # dedicated primary column above instead -- zeroed here per
            # Grant so it isn't shown in both places. Total is left
            # untouched (still the true whole-cycle spend figure).
            row["Anti-Abdul"] = 0.0
        else:
            row["Anti-Abdul (primary only)"] = 0.0
    _write_sheet(rows, OUTPUT_COLUMNS_1M, GRAPHICS_SHEET_ID, credentials_path, worksheet_name, blank_zeros=True)
    return rows


def update_all_groups_chart(output_dir, sheet_id, credentials_path, worksheet_name="SEN_groups_chart_ALL"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")
    rows = build_all_rows(output_dir)
    _write_sheet(rows, ALL_OUTPUT_COLUMNS, GRAPHICS_SHEET_ID, credentials_path, worksheet_name, blank_zeros=True)
    return rows
