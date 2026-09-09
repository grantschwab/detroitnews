# Start / Stop Commands

Four separate scripts, run as four separate supervised processes:

- **`monitor.py --quarter Q2`** — candidate committee filings (actively tracked quarter) → `cands_Q2` tab
- **`outside_spending.py`** — outside-group (Super PAC/party) spending, general election (10 nominees across 5 races, `candidates_general.csv`) → `outside_spend_gen` tab. (Pre-Aug-4 primary data is frozen in `outside_spend_primary`, no longer updated.)
- **`monitor_preprimary.py`** — 12-day pre-primary (12P) reports → `cands_preprimary` tab
- **`monitor.py --quarter Q1`** — past-quarter amendment recheck, uses `--raw-subdir raw_q1 --state-file .monitor_state_q1.json` so it never collides with the Q2 instance's data → `cands_Q1` tab

All run through `supervise.sh`, which auto-restarts any script if it crashes (with a macOS notification each time), and gives up after 5 crashes within 30 minutes rather than looping forever.

See `GUIDE.md` → "Past-Quarter Amendment Rechecking" for why the Q1 instance exists and how to roll it forward once Q3 becomes the actively-tracked quarter.

---

## Start

```bash
cd "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/code"
export FEC_API_KEY="qTG3dgVaQs5TjGq9Z2yQEH71weeD4pP3W1c0Jpnx"
rm -f ../monitor_q2.stop ../outside_spending_q2.stop ../monitor_q1.stop ../monitor_preprimary.stop

nohup caffeinate -i ./supervise.sh monitor ../monitor_q2.stop 5 1800 \
  python3 -u monitor.py --candidates candidates.csv --output-dir '..' \
  --cycle 2026 --quarter Q2 --worksheet "cands_Q2" --poll-interval 604800 \
  > ../monitor_q2.log 2>&1 &

nohup caffeinate -i ./supervise.sh outside_spending ../outside_spending_gen.stop 5 1800 \
  python3 -u outside_spending.py --candidates candidates_general.csv --output-dir '..' \
  --cycle 2026 --worksheet "outside_spend_gen" --poll-interval 1800 \
  > ../outside_spending_gen.log 2>&1 &

nohup caffeinate -i ./supervise.sh preprimary ../monitor_preprimary.stop 5 1800 \
  python3 -u monitor_preprimary.py --candidates candidates.csv --output-dir '..' \
  --worksheet cands_preprimary \
  > ../monitor_preprimary.log 2>&1 &

nohup caffeinate -i ./supervise.sh monitor_q1 ../monitor_q1.stop 5 1800 \
  python3 -u monitor.py --candidates candidates.csv --output-dir '..' \
  --cycle 2026 --quarter Q1 --worksheet "cands_Q1" --poll-interval 604800 \
  --raw-subdir raw_q1 --state-file .monitor_state_q1.json \
  > ../monitor_q1.log 2>&1 &
```

## Stop (all four)

```bash
cd "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526"
touch monitor_q2.stop outside_spending_gen.stop monitor_q1.stop monitor_preprimary.stop
pkill -f "supervise.sh|monitor.py|outside_spending.py|monitor_preprimary.py"
```

**Always use this exact stop command, not a plain `pkill -f monitor.py`.** The supervisor will otherwise just relaunch what you killed within 5 seconds.

## Stop just one

```bash
# Q2 candidate filing monitor
touch monitor_q2.stop && pkill -f "supervise.sh monitor |monitor.py --candidates candidates.csv --output-dir .. --cycle 2026 --quarter Q2"

# Outside spending
touch outside_spending_gen.stop && pkill -f "supervise.sh outside_spending |outside_spending.py"

# Pre-primary
touch monitor_preprimary.stop && pkill -f "supervise.sh preprimary |monitor_preprimary.py"

# Q1 amendment recheck
touch monitor_q1.stop && pkill -f "supervise.sh monitor_q1 |monitor.py --candidates candidates.csv --output-dir .. --cycle 2026 --quarter Q1"
```

---

## Checking on it

```bash
tail -f "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/monitor_q2.log"
tail -f "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/outside_spending_gen.log"
tail -f "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/monitor_preprimary.log"
tail -f "/Users/grantschwab/Desktop/Detroit News/Data/campaign_finance/instance/FEC_071526/monitor_q1.log"

ps aux | grep -E "supervise.sh|monitor.py|outside_spending.py|monitor_preprimary.py" | grep -v grep
```

You should see 12 processes total when all four are running (4× `caffeinate`, 4× `supervise.sh`, 4× `python3`).

---

## Auto-start on machine reboot (launchd) — built, not yet active

Four LaunchAgent plists already exist at `~/Library/LaunchAgents/com.grantschwab.fec.{outside_spending,preprimary,monitor_q2,monitor_q1}.plist`, one per process above, each `RunAtLoad`-enabled with the same commands as the manual start section. They are currently **unloaded** (not running) because macOS TCC blocks `launchd`-spawned processes from accessing anything under `~/Desktop/` — this project's whole directory tree — even though Terminal.app itself has access. Symptom when tested: all 4 exit immediately with code 78, and a minimal diagnostic agent surfaced the real error, `getcwd: cannot access parent directories: Operation not permitted`. This is a hard macOS security boundary, not something fixable in code or plist config.

**To activate** (one-time, manual, only Grant can do this):
1. System Settings → Privacy & Security → Full Disk Access
2. Click "+", press `Cmd+Shift+G`, type `/bin/bash`, add it
3. Then bootstrap all four:
   ```bash
   for f in ~/Library/LaunchAgents/com.grantschwab.fec.*.plist; do
     launchctl bootstrap gui/$(id -u) "$f"
   done
   ```
4. Confirm via `launchctl list | grep grantschwab.fec` and by tailing the four log files after the next reboot.

Until this is done, a reboot kills all `nohup`/`caffeinate` processes started manually above, and they must be restarted by hand (the Start section) — they do **not** currently survive a restart.
