# Bujairami Stock Monitor (GitHub Actions)

Runs the FragranceNet Bujairami monitor on GitHub's servers every 15 minutes,
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
  15-minute cron.
- Each run reads/writes `bujairami_state.json` and commits it back, so the next
  run remembers what was in stock last time. Alerts fire only on real changes.
- Times in the event log are Pacific (set via `TZ` in the workflow).
- **Health ping:** if a run comes back blocked, empty, or only partial, the bot
  sends a Discord "went quiet" alert once (and again every 6 hours if it stays
  down), then a "recovered" alert when checks work again. State for this lives in
  `bujairami_health.json`, committed alongside the snapshot. This is so silence
  never gets mistaken for "nothing in stock."

## Notes

- Runs on Azure datacenter IPs, which FragranceNet's anti-bot may block more than
  a home IP. The script fails safe (skips a cycle rather than sending false
  alerts). `cloudscraper` is installed to help. If the Actions logs show it never
  finds products, the runner IP is being blocked -- fall back to running on a home
  IP (PC or Raspberry Pi).
