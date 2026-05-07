# Michigan Polymarket Election Monitor

Pulls prediction market data for Michigan election races from [Polymarket](https://polymarket.com), a USDC-denominated prediction market platform. Produces a timestamped CSV snapshot of all active Michigan markets — primaries and general elections — with per-candidate odds and money figures.

---

## What it captures

Each run produces one CSV file in `data/` named `snapshot_YYYYMMDD_HHMMSS.csv`. Each row is one race (event). Columns include:

| Column | Description |
|---|---|
| `pulled_at` | UTC timestamp of the pull |
| `event_title` | Race name as listed on Polymarket |
| `event_end_date` | Resolution date (primary or general election day) |
| `event_volume_usd` | Total money wagered on this race (from the API's event-level field) |
| `total_market_volume_usd` | Sum of volume across all individual candidate/party markets — see note below on double-counting |
| `event_liquidity_usd` | Liquidity available in the automated market maker |
| `event_open_interest_usd` | Open positions not yet resolved |
| `named_candidates` | Count of real candidates/parties (excludes placeholders) |
| `placeholder_slots` | Count of unfilled slots Polymarket pre-built |
| `candidate_N` / `prob_N` / `vol_N_usd` | Candidate or party name, win probability (0–1), and money wagered on that outcome — repeated for each named candidate, sorted by probability descending |

Running the script multiple times accumulates snapshots, allowing you to track how odds and volume move over time.

---

## How to run

No dependencies beyond the Python standard library.

```
python polymarket_mi.py
```

Output is printed to the terminal (summary table) and written to `data/`.

---

## How the data is sourced

### The API

Polymarket exposes a public REST API through two base URLs:

- **Gamma API** (`gamma-api.polymarket.com`) — event and market metadata, including titles, candidate questions, volumes, prices, and tags. This is what the script uses.
- **CLOB API** (`clob.polymarket.com`) — the live order book, useful for real-time bid/ask spreads. Not used here but available if finer price resolution is needed.

No authentication or API key is required for either. However, the API blocks requests that use Python's default `urllib` user-agent with a `403 Forbidden` response. The script sets a browser-like `User-Agent` header to get around this.

### How Michigan races are found

Polymarket tags events with category labels. Two tags reliably cover Michigan election markets as of May 2026:

| Tag ID | Label | Covers |
|---|---|---|
| `1433` | Michigan Primary | Democratic and Republican primary races |
| `104024` | Michigan Midterm | General election races (party vs. party) |

The script fetches all events under both tags, then deduplicates by event ID. As a fallback, it also runs keyword searches (`"Michigan"`, `"MI-10"` through `"MI-14"`) and filters results for Michigan-matching slugs or titles. This catches any races that Polymarket staff tagged inconsistently.

**Important:** Tag IDs are not documented anywhere by Polymarket. They were discovered by fetching a known event (the MI-10 Republican Primary) by slug and inspecting its tag array. If new Michigan markets appear and aren't being picked up, fetch a known example and check whether a new tag ID was used.

### Event structure

Each Polymarket **event** (race) contains multiple **markets** — one per candidate or party. For primaries, each market asks "Will [Candidate] win/be the nominee?" For general elections, the format is "[Party] wins [race]." Probabilities come from the `outcomePrices` field on each market, which represents the Yes/No prices in a binary market (Yes price ≈ implied win probability).

---

## Data pitfalls

### Both `outcomes` and `outcomePrices` are JSON strings, not arrays

Even though they look like lists in the Polymarket web UI, the API returns these fields as serialized JSON strings — e.g., `"[\"Yes\", \"No\"]"` — not native arrays. The script parses both with `json.loads()`. If you write any additional code against this API, don't assume these are already lists.

### Two volume fields that don't always agree

The API returns `volume` at the event level and also a `volume` field on each individual market. Summing the market-level volumes does not always match the event-level figure. Polymarket appears to count some volume differently at the two levels (possibly due to AMM vs. CLOB trading or how they handle resolved markets). The CSV includes both: `event_volume_usd` (API's event-level figure) and `total_market_volume_usd` (sum of market volumes). Use `event_volume_usd` as the primary money figure; it appears more reliable.

### Placeholder candidates

Before real candidates file paperwork, Polymarket pre-builds placeholder slots named "Person A," "Person B," etc. for primaries and "Option B," "Option C," etc. for generals. These slots carry zero or near-zero volume and no real odds. The script filters them out of the named candidate columns and counts them separately in `placeholder_slots`. If a real candidate files and their name doesn't yet appear, they may be hiding inside one of these placeholders.

### Candidates not on the ballot

Polymarket listed named candidates in some races who did not ultimately qualify for the ballot (observed in the MI-10 Republican Primary). These appear as named candidates with real odds and volume — they are not marked differently in the API. Cross-referencing candidate names against official filing records is necessary to identify them.

### Candidate name parsing

Candidate names are extracted from question text using regex, not a structured field. Two formats exist:

- **Primary:** `"Will [Name] win/be the [party] nominee for [race]?"` → extracts everything between "Will" and "win/be"
- **General:** `"[Party] wins [race] [year]"` → extracts everything before "wins"

This works reliably but has edge cases. In one instance, "Jocelyn Benson" originally parsed as just "Jocelyn" because the question phrasing cut off unexpectedly. If candidate names look truncated or wrong in the output, check the raw `market_question` field in the API response for that event.

General election party labels come through with articles: "the Democrats," "the Democratic Party," "the Republican Party." This is Polymarket's question phrasing, not a parsing error.

### The keyword search (`q` parameter) does not filter reliably

The Gamma API's `q` parameter does not behave like a full-text search. In testing, querying `q=Michigan` returned unrelated markets (NBA games, geopolitical events) rather than Michigan election markets. The script uses keyword search only as a fallback, with a secondary filter (`is_michigan()`) that checks the event slug and title for Michigan-related strings. Tag-based search is the reliable primary method.

### General election markets have no named candidates yet

As of the initial build (May 2026), all general election markets list only party-level outcomes (Democrat, Republican, Independent). Named candidate slots exist as placeholders and will presumably be filled after the August 4 primary. The script handles this correctly — party names parse as the "candidate" field in the wide format.

---

## Extending the script

**Add more races (state leg, local):** Find one example event on Polymarket, fetch it from the Gamma API by slug, and check its tag array for any new Michigan-specific tag IDs. Add those IDs to `MICHIGAN_TAG_IDS`.

**Add more keyword fallbacks:** The current list covers MI-10 through MI-14. If districts MI-01 through MI-09 start appearing under different slugs not caught by the tag search, add them to the keyword list in `main()`.

**Switch to scheduled pulls:** The script is designed to run manually. Each run produces an independent timestamped file, so results stack naturally. To automate, wrap the script in a cron job or use the Claude Code `schedule` feature.

**Kalshi:** The other major prediction market platform for elections. Requires account creation to access full API data. Kalshi is CFTC-regulated, which affects which markets they can offer and how they're structured — different from Polymarket's approach.
