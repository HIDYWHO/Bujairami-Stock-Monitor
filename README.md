# Bujairami Stock Monitor

Watches the FragranceNet Bujairami listing and pings Discord when something
comes back in stock or a new listing shows up.

It runs in two places:

- **My PC (main bot).** Task Scheduler runs `python bujairami_monitor.py --once`
  every 10 minutes, plus a watch-mode loop. This is the copy that works.
- **GitHub Actions (best-effort backup).** Runs the same script on a cron so
  there is some coverage when the PC is off. Two known limits:
  - GitHub's runner IPs are currently refused by FragranceNet (HTTP 403), so
    these runs fail.
  - GitHub only fires the 5-minute cron every few hours in practice.

Both post to the same Discord channel. Every alert ends with `[PC]` or
`[GitHub]` so I can tell which bot sent it.

## Setup (GitHub Actions)

1. Push these files to a GitHub repo (public = unlimited free minutes).
2. Repo **Settings > Secrets and variables > Actions > New repository secret**
   - Name: `BUJAIRAMI_DISCORD_WEBHOOK`
   - Value: my Discord webhook URL
3. Repo **Settings > Actions > General > Workflow permissions** > select
   **Read and write permissions** > Save. (Lets the job push `bujairami_state.json` back.)
4. **Actions** tab > "Bujairami Stock Monitor" > **Run workflow** to test.

On the PC, set `BUJAIRAMI_DISCORD_WEBHOOK` as an environment variable and run
the script from the repo folder.

## Where state lives

- **PC:** `local_data/` in the repo folder (git-ignored). Holds
  `bujairami_state.json`, `bujairami_deep.json`, `bujairami_health.json`,
  `bujairami_events.log`, `bujairami_pending.json` (Discord messages waiting
  to be re-sent) and the lock file.
- **GitHub Actions:** the repo root. The workflow commits those files back
  after each run, so the next run remembers what was in stock last time.
- `BUJAIRAMI_DATA_DIR` overrides both.
- `bujairami_watchlist.json` is shared config and stays in the repo root.

One-time copy: if `local_data/` has no state file yet but the repo root has
`bujairami_state.json`, the first run copies the root state files into
`local_data/` (copy, not move) and logs one line. After that the PC never
touches the root files.

## How it runs

- `.github/workflows/monitor.yml` runs `python bujairami_monitor.py --once` on a
  5-minute cron (offset to :02/:07/... to dodge the top-of-hour rush).
- Each run diffs the listing against the saved state. Alerts fire only on real
  changes.
- Times in the event log are Pacific (set via `TZ` in the workflow).
- **Health ping:** if checks keep failing (blocked, empty, or partial) for 60+
  minutes, the bot sends a Discord "went quiet" alert (again every 6 hours if it
  stays down; the GitHub copy sends no reminders), then a "recovered" alert when
  checks work again. Shorter blips just get one line in the event log with the actual
  error. State for this lives in `bujairami_health.json`. This is so silence
  never gets mistaken for "nothing in stock."
- **Anti-flap:** a product that drops out of the listing for a run is kept in
  state for 48 hours, so it doesn't come back as a fake NEW LISTING. A product
  has to read sold out 2 runs in a row before it counts as sold out, so a
  one-run blip doesn't cause a fake BACK IN STOCK.
- **Unsent alerts:** if Discord can't be reached (network down, Discord
  errors, a rate limit that won't clear, a missing webhook), the message is
  saved to `bujairami_pending.json` and re-sent at the start of the next run,
  oldest first, marked "delayed". It keeps trying for 24 hours, then gives up
  with a line in the event log. Before this, a failed post was lost for good.
- **Product page rotation:** every run checks the product page of each
  sold-out watchlist item, plus a rotation through the other sold-out
  products. The rotation always gets at least 2 slots, even when the
  watchlist fills the whole batch. (From 2026-11-11 to 11-13 it got none:
  12 sold-out watchlist items against a batch of 8.)
- State files are written to a temp file and then swapped in, so a run that
  gets killed mid-write can't leave a half-written file.

## Tuning (env vars)

Set these in `monitor.yml` for GitHub, or as environment variables on the PC.

| Variable | Default | What it does |
|---|---|---|
| `HEALTH_ALERT_AFTER_MIN` | 60 | Minutes of failed checks before a Discord "went quiet" ping (0 = old behavior) |
| `HEALTH_REMINDER_HOURS` | 6 | Hours between "still down" reminders (0 = none). `monitor.yml` sets 0, since GitHub can be blocked for days |
| `MISSING_GRACE_HOURS` | 48 | How long a product can be missing from the listing before it's forgotten |
| `SOLDOUT_CONFIRM_RUNS` | 2 | Runs in a row a product must read sold out before it counts (1 = old behavior) |
| `DEEP_CHECK_BATCH` | 8 (`monitor.yml` sets 12) | Product pages deep-checked per run (watchlist + rotation) |
| `DEEP_CHECK_ROTATE_MIN` | 2 | Rotation slots every run gets even when the watchlist fills the batch (capped at the batch; 0 = old behavior) |
| `DISCORD_RETRY_HOURS` | 24 | How long an undelivered Discord message keeps being re-sent (0 = don't save it) |
| `STALE_PAGE_RETRIES` | 8 | Re-fetches for a listing page served by an out-of-date FragranceNet server |
| `BACK_ALERT_COOLDOWN_MIN` | 120 | Minimum minutes between BACK IN STOCK pings for the same product (0 = off) |
| `BUJAIRAMI_DATA_DIR` | `local_data` (PC), repo root (Actions) | Folder for the state files and lock |
| `BUJAIRAMI_RUNNER` | `PC`, or `GitHub` on Actions | Label added to the end of every alert subject |

## Pushing code changes from the PC

PC state now lives in `local_data/`, so the working tree stays clean. To push:

    git pull --rebase
    git push

A watch-mode loop keeps running the old code. Stop it and start it again to
pick up the new code. (`--once` runs from Task Scheduler pick it up on their
next run.)

## Tests

    python -m unittest discover -s tests

The tests run offline: no network and no Discord. They also run on GitHub on
every code push (`.github/workflows/tests.yml`).

## Stale pages (fixed 2026-11-10)

After a restock, FragranceNet's servers don't all update at once. Each listing
page includes the product total its server knows about (`numOfResults`), so
the monitor re-fetches any page reporting a lower total than the newest one,
until every page agrees. Before this, page 5 kept coming from an old server
and the monitor sat at 103 while the site had 115. The site total from each
good run is saved in `bujairami_health.json` (`products` vs `site_total`).

Retries drop Cloudflare's `__cflb` cookie first. That cookie pins a session to
one backend server, so without dropping it every retry hit the same stale one.

The servers also disagree about **stock** even when the totals match (Tea
Splash read "in stock" on about 30% of listing loads after it sold out). Product
pages are consistent, so `deep_check.verify_changes()` checks the product page
for every watchlist item each run, for any product whose listing stock differs
from the saved state, and for new listings. The product page wins in both
directions. A page-confirmed sold out applies right away, without the 2-run wait.
Non-watchlist verdicts are cached for `VERIFY_CACHE_MIN` (5) minutes.

## Notes

- GitHub runs on Azure datacenter IPs, and FragranceNet currently answers them
  with 403. The script fails safe (skips a cycle rather than sending false
  alerts) and the health ping reports it once. The PC on a home IP is the
  reliable copy.
