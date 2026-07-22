"""
groupspend.py

Per-committee breakdown for the Stevens vs. El-Sayed race, feeding two
Flourish graphics alongside overallspend_chart's summary view:

- SEN_groups_chart_100k+: outside groups only, $100k+ total spend only,
  for a bar chart.
- SEN_groups_chart_ALL: every outside group (any spend amount) plus both
  campaigns, for a reference table.

Same pattern as overallspend.py: reads only the already-compiled output
CSVs written by monitor.py (campaign_finance_2026_Q2.csv, ALL tab only)
and outside_spending.py (outside_spending_2026.csv) -- no new API calls.
Called at the end of each of those two scripts' own sheet-upload step.
"""

import csv
import os

try:
    import gspread
    from google.oauth2.service_account import Credentials as ServiceAccountCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

OUTPUT_COLUMNS = ["Group", "Pro-Haley", "Anti-Abdul", "Pro-Abdul", "Anti-Haley"]
ALL_OUTPUT_COLUMNS = ["Group", "Supports", "Pro-candidate", "Anti-opponent", "Total"]

# Graphics tabs live in a separate spreadsheet from the main tracking
# sheet (candidate filings, raw outside-spending data) -- Flourish-facing
# only. Ignores whatever --sheet-id the caller passes for its own tab.
GRAPHICS_SHEET_ID = "1H2aq1gKbCV-9jcDs5ee2wIJeQdOAIeMQ_iLm1RbLUgY"

# Maps (candidate slug, Support/Oppose) -> which of the four spend columns it feeds
CATEGORY_COLUMN = {
    ("stevens", "Support"): "Pro-Haley",
    ("elsayed", "Oppose"):  "Anti-Abdul",
    ("elsayed", "Support"): "Pro-Abdul",
    ("stevens", "Oppose"):  "Anti-Haley",
}

GROUP_COLUMNS = ["Pro-Haley", "Anti-Abdul", "Pro-Abdul", "Anti-Haley"]
VALUE_COLUMNS = GROUP_COLUMNS

# FEC committee names are filed in ALL CAPS. Known acronyms stay uppercase;
# articles/prepositions lowercase except as the first word; everything else
# gets normal Title Case.
ACRONYMS = {"PAC", "PAF", "UDP", "AP", "DMFI", "JDCA", "LLC", "GOP", "DNC",
            "RNC", "AIPAC", "NRA", "UAW"}
LOWERCASE_WORDS = {"a", "an", "the", "of", "for", "to", "in", "and", "or",
                    "on", "at", "by", "from", "with"}

# Stylized brand names that don't follow normal Title Case (e.g. internal
# capitals). Checked case-insensitively against each raw word before the
# generic formatting rules below.
BRAND_OVERRIDES = {"MOVEON.ORG": "MoveOn.org"}


def format_group_name(name):
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
        elif core.upper() in ACRONYMS:
            formatted = core.upper()
        elif i != 0 and core.lower() in LOWERCASE_WORDS:
            formatted = core.lower()
        elif "." in core:
            parts = core.split(".")
            formatted = parts[0].capitalize() + "." + ".".join(p.lower() for p in parts[1:])
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
    path = os.path.join(output_dir, "output", "campaign_finance_2026_Q2.csv")
    labels = {"stevens": "Stevens campaign", "elsayed": "El-Sayed campaign"}
    result = {"Stevens campaign": 0.0, "El-Sayed campaign": 0.0}
    if not os.path.exists(path):
        return result
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("Candidate Name", "").strip().lower()
            if name in labels:
                result[labels[name]] = _to_float(row.get("C Expenditures"))
    return result


MIN_TOTAL = 100000


def _lean(values):
    """Which candidate a group's spending predominantly helps.
    Pro-Haley + Anti-Abdul both help Stevens; Pro-Abdul + Anti-Haley both
    help El-Sayed."""
    haley_side = values.get("Pro-Haley", 0.0) + values.get("Anti-Abdul", 0.0)
    abdul_side = values.get("Pro-Abdul", 0.0) + values.get("Anti-Haley", 0.0)
    return "Stevens" if haley_side >= abdul_side else "El-Sayed"


def build_rows(output_dir):
    group_rows = _group_rows(output_dir)

    rows = []
    for group, values in group_rows.items():
        rows.append({"Group": format_group_name(group), **values,
                     "Total": sum(values[c] for c in VALUE_COLUMNS)})

    rows = [r for r in rows if r["Total"] >= MIN_TOTAL]
    rows.sort(key=lambda r: r["Total"], reverse=True)
    return rows


def build_all_rows(output_dir):
    """Every outside group (any spend amount) plus both campaigns -- for
    the unfiltered reference table, not the $100k+ chart. Pro-Haley/
    Anti-Abdul collapse into "Pro-candidate"/"Anti-opponent" (and
    Pro-Abdul/Anti-Haley the same way), with "Supports" saying which
    candidate they refer to -- two fewer columns than showing all four
    directions separately."""
    group_rows = _group_rows(output_dir)
    campaign_values = _campaign_values(output_dir)

    rows = []
    campaign_supports = {"Stevens campaign": "Stevens", "El-Sayed campaign": "El-Sayed"}
    for label, spend in campaign_values.items():
        rows.append({"Group": label, "Supports": campaign_supports[label],
                     "Pro-candidate": 0.0, "Anti-opponent": 0.0, "Total": spend})
    for group, values in group_rows.items():
        supports = _lean(values)
        if supports == "Stevens":
            pro, anti = values["Pro-Haley"], values["Anti-Abdul"]
        else:
            pro, anti = values["Pro-Abdul"], values["Anti-Haley"]
        # Total sums all four raw categories, not just the dominant-side
        # pair shown -- protects against undercounting if a group ever
        # spends on both candidates at once (not seen in practice, but
        # Pro-candidate/Anti-opponent alone wouldn't capture it).
        total = sum(values[c] for c in GROUP_COLUMNS)
        rows.append({"Group": format_group_name(group), "Supports": supports,
                     "Pro-candidate": pro, "Anti-opponent": anti, "Total": total})

    rows.sort(key=lambda r: r["Total"], reverse=True)
    return rows


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
    rows = build_rows(output_dir)
    _write_sheet(rows, OUTPUT_COLUMNS, GRAPHICS_SHEET_ID, credentials_path, worksheet_name)
    return rows


def update_all_groups_chart(output_dir, sheet_id, credentials_path, worksheet_name="SEN_groups_chart_ALL"):
    if not GSPREAD_AVAILABLE:
        raise RuntimeError("gspread not installed (pip install gspread google-auth)")
    rows = build_all_rows(output_dir)
    _write_sheet(rows, ALL_OUTPUT_COLUMNS, GRAPHICS_SHEET_ID, credentials_path, worksheet_name, blank_zeros=True)
    return rows
