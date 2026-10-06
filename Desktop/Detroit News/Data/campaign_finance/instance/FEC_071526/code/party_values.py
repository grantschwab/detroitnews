"""Party coordinated spending read from the local coordinated_spending_2026.csv (no FEC calls).

Used by POST_PRIM_overview, SEN_race_totals and RogersElSayed_test so they can refresh
without hitting the FEC API. Written by coordinated_spend.update() each cycle.
"""
import csv
import os

PARTY_TO_SIDE = {"DSCC": "D", "DCCC": "D", "NRSC": "R", "NRCC": "R"}


def _rows(output_dir):
    path = os.path.join(output_dir, "output", "coordinated_spending_2026.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _key(name):
    return "".join(c for c in name.lower() if c.isalnum())


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def for_candidate(output_dir, last_name, party_side, periods=("Since Aug 1 Spent",)):
    """Sum of coordinated spending for one candidate from committees on one side (D or R)."""
    total = 0.0
    for r in _rows(output_dir):
        if _key(r["Candidate Name"]) != _key(last_name):
            continue
        if PARTY_TO_SIDE.get(r["Party Committee"]) != party_side:
            continue
        total += sum(_f(r.get(p)) for p in periods)
    return total


def senate_total(output_dir):
    """Senate-race coordinated spending (Contest ID mi00), all committees."""
    return sum(_f(r["SUM CandCategory"]) for r in _rows(output_dir) if r["Contest ID"] == "mi00")
