# 🥇 Gold Market Tracker

Automated gold market intelligence, updated straight into this repository by
GitHub Actions — no server required.

| Schedule | Report | Data | Model |
|---|---|---|---|
| ⏱️ Every 15 minutes | [`LATEST_15MIN_REPORT.md`](LATEST_15MIN_REPORT.md) | Intraday price (yfinance / Yahoo), retail positioning, news RSS | Claude Haiku |
| 📅 Daily @ 23:00 UTC | [`LATEST_DAILY_REPORT.md`](LATEST_DAILY_REPORT.md) | CME Volume/Open Interest, GLD ETF tonnage | Claude Sonnet |

Every data fetch is layered — primary source → secondary source → last-known
cache → structured fallback notice — so the pipeline **never crashes** and the
reports below are always current or clearly marked as degraded.

> ⚠️ Auto-generated content. Not investment advice.

---

## ⏱️ Live 15-Minute Sentiment

<!-- 15MIN_REPORT:START -->
*Waiting for the first scheduled run…*
<!-- 15MIN_REPORT:END -->

---

## 📅 Latest Daily Macro Report

<!-- DAILY_REPORT:START -->
# 📅 Gold — Daily Macro Report

**Last updated:** 2026-08-25 03:14:44 UTC

## 🏦 CME Gold Futures — Volume & Open Interest

> ⚠️ **Fallback state:** all CME volume/OI sources (and the cache) were unavailable this run. The next scheduled run will retry automatically.

## 🪙 GLD ETF Holdings (Tonnage)

> ⚠️ **Fallback state:** all GLD tonnage sources (and the cache) were unavailable this run. The next scheduled run will retry automatically.

## 🤖 AI Analysis (Claude Sonnet)

> ⚠️ AI analysis was unavailable this run (API error or missing `ANTHROPIC_API_KEY`). Raw data above is still current.

---
*Auto-generated daily by [`scripts/daily_macro.py`](scripts/daily_macro.py). Not investment advice.*
<!-- DAILY_REPORT:END -->

---

## How It Works

```
.github/workflows/15min_sentiment.yml ──▶ scripts/15min_sentiment.py ──▶ LATEST_15MIN_REPORT.md ─┐
.github/workflows/daily_macro.yml     ──▶ scripts/daily_macro.py     ──▶ LATEST_DAILY_REPORT.md ─┤
                                                                                                 └▶ README.md (this dashboard)
```

- Each script fetches data with realistic browser headers, strict timeouts,
  and try/except around **every** network call.
- Successful fetches are cached in `data/` (committed by the workflow) so a
  future outage can fall back to the last-known values.
- Claude analyzes the raw data and writes the narrative sections; if the API
  is unavailable, the raw-data report is still written.
- The workflow commits the updated report files and splices them into this
  README between HTML comment markers.

## Setup

1. Push this repository to GitHub.
2. Add your Anthropic API key as a repository secret named
   **`ANTHROPIC_API_KEY`** (Settings → Secrets and variables → Actions).
3. Enable workflow write access if needed (Settings → Actions → General →
   Workflow permissions → "Read and write permissions").
4. Trigger either workflow manually from the **Actions** tab
   (`workflow_dispatch`) or wait for the next scheduled run.

> Note: GitHub schedules cron workflows on a best-effort basis — the
> "every 15 minutes" job may occasionally run a few minutes late.
