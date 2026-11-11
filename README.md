# Bujairami Stock Monitor (GitHub Actions)

Runs the FragranceNet Bujairami monitor on GitHub's servers every 5 minutes,
so it keeps checking even when my PC is off. Free on a public repo.

## Setup

1. Push these files to a new GitHub repo (public = unlimited free minutes).
2. Repo **Settings > Secrets and variables > Actions > New repository secret**
   - Name: `BUJAIRAMI_DISCORD_WEBHOOK`
   - Value: my Discord webhook URL
3. Repo **Settings > Actions > General > Workflow permissions** > select
   **Read and write permissions** > Save. (Lets the job push `bujairami_state.json` back.)
4. **Actions** tab > "Bujairami Stock Monitor" > **Run workflow** to test.

## How it runs

- `.github/workflows/monitor.yml` runs `python bujairami_monitor.py --once` on a
  5-minute cron (offset to :02/:07/... to dodge the top-of-hour rush).
- Each run reads/writes `bujairami_state.json` and commits it back, so the next
  run remembers what was in stock last time. Alerts fire only on real changes.
- Times in the event log are Pacific (set via `TZ` in the workflow).
- **Health ping:** if checks keep failing (blocked, empty, or partial) for 60+
  minutes, the bot sends a Discord "went quiet" alert (again every 6 hours if it
  stays down), then a "recovered" alert when checks work again. Shorter blips
  just get one line in the event log with the actual error. State for this lives
  in `bujairami_health.json`. This is so silence never gets mistaken for
  "nothing in stock."
- **Anti-flap:** a product that drops out of the listing for a run is kept in
  state for 48 hours, so it doesn't come back as a fake NEW LISTING. A product
  has to read sold out 2 runs in a row before it counts as sold out, so a
  one-run blip doesn't cause a fake BACK IN STOCK.

## Tuning (env vars in monitor.yml)

| Variable | Default | What it does |
|---|---|---|
| `HEALTH_ALERT_AFTER_MIN` | 60 | Minutes of failed checks before a Discord "went quiet" ping (0 = old behavior) |
| `MISSING_GRACE_HOURS` | 48 | How long a product can be missing from the listing before it's forgotten |
| `SOLDOUT_CONFIRM_RUNS` | 2 | Runs in a row a product must read sold out before it counts (1 = old behavior) |
| `DEEP_CHECK_BATCH` | 12 | Product pages deep-checked per run (watchlist + rotation) |
| `STALE_PAGE_RETRIES` | 8 | Re-fetches for a listing page served by an out-of-date FragranceNet server |
| `BACK_ALERT_COOLDOWN_MIN` | 120 | Minimum minutes between BACK IN STOCK pings for the same product (0 = off) |

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

- Runs on Azure datacenter IPs, which FragranceNet's anti-bot may block more than
  a home IP. The script fails safe (skips a cycle rather than sending false
  alerts). `cloudscraper` is installed to help. If the Actions logs show it never
  finds products, the runner IP is being blocked -- fall back to running on a home
  IP (PC or Raspberry Pi).
