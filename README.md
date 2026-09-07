# fast-market-data.

Gold-focused market microstructure engine with 15-minute GitHub Actions
automation. Rebuilds the buyer-vs-seller volume footprint for gold (spot
XAU/USD, with optional COMEX GC true volume), derives volume nodes, CVD,
session VWAP, absorption/exhaustion flags, and cross-asset correlations, and
appends each snapshot to [`market_updates.txt`](market_updates.txt).

## Architecture

```
├── .github/workflows/
│   ├── market_monitor.yml   # */15 cron: run snapshot, commit market_updates.txt
│   └── ci.yml               # pytest on push/PR
├── src/
│   ├── adapters/            # OANDA (spot/FX/indices), CME/COMEX (GC tape), IBKR
│   ├── engine/              # aggressor logic, price-level footprint, volume nodes
│   ├── resilience/          # backoff+jitter, token-bucket limiter, schema guard
│   ├── indicators/          # CVD & divergence, session VWAP, absorption, correlations
│   ├── storage/             # market_updates.txt appender, JSON cache
│   ├── snapshot.py          # REST snapshot orchestration (cron mode)
│   └── main.py              # shim → root main.py
├── tests/                   # unit tests (pytest)
├── config/                  # env-driven settings (.env supported locally)
└── main.py                  # CLI entry point
```

## How the footprint is built

* **COMEX GC (centralized true volume)** — when `CME_API_URL` points at a
  licensed trade feed, exact matched sizes are mapped Bid-vs-Ask using the
  exchange **aggressor flags** (`B` = buyer aggressor / traded at the ask,
  `S` = seller aggressor / traded at the bid).
* **Spot XAU/USD, forex, index CFDs (decentralized tick volume)** — no
  consolidated tape exists, so aggressor side is inferred with the **tick
  rule**: uptick = buy, downtick = sell, unchanged price inherits the last
  direction. In snapshot mode this is applied to 1-minute candle closes,
  which is an approximation of true order flow — treat the spot CVD as
  directional pressure, not matched volume.

Every trade/candle is bucketed onto a tick-quantized price grid tracking
**Total / Buy / Sell volume and Net Delta** per level, from which the engine
derives **POC**, **Value Area** (70% expansion from POC), **HVN**, and
**LVN**.

## Indicators

* **CVD + divergence** — flags price printing a window high while CVD fails
  to confirm (bearish), and the mirror case (bullish).
* **Session VWAP** — resets exactly at Tokyo 00:00, London 07:00,
  New York 13:30, Sydney 21:00 (UTC).
* **Absorption / exhaustion** — heavy volume with minimal range
  (absorption), or a push to a window extreme with delta leaning against the
  move (exhaustion).
* **Correlations** — gold 1-minute log-returns vs. a **DXY proxy** (ICE
  formula over the six component pairs) and index CFDs (SPX500, NAS100,
  US30).

## Resilience (why this doesn't crash the cron)

* `retry_with_backoff` — exponential backoff with jitter on 429/5xx/network
  drops, honoring `Retry-After`; non-transient errors (401/404) fail fast.
* `TokenBucket` — a per-minute request governor every REST call must pass
  through (`REQUESTS_PER_MINUTE`, default 60).
* `schema_guard` — every incoming JSON record is validated; malformed quotes
  are dropped and counted, never fatal.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env        # add your OANDA_API_KEY
python main.py --mode snapshot --lookback 15m
```

`python src/main.py ...` works identically. Useful flags: `--no-append`
(print only), `--output FILE`, `-v`.

Run tests with:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## Automation

[`market_monitor.yml`](.github/workflows/market_monitor.yml) runs every 15
minutes (and on manual `workflow_dispatch`): checkout → Python 3.11 →
`pip install` → `python main.py --mode snapshot --lookback 15m` →
`git add market_updates.txt && git commit -m "Auto-update market data" && git push`
to `main` with the standard `GITHUB_TOKEN`.

### Required repository secrets

| Secret | Purpose |
| --- | --- |
| `OANDA_API_KEY` | OANDA v20 token (practice or live) — required |
| `OANDA_ENV` | `practice` (default) or `live` |
| `CME_API_URL` / `CME_API_KEY` | optional licensed COMEX GC trade feed |

Without `OANDA_API_KEY` the run logs a warning and exits 0 (no red cron
runs, no commit). If the market is closed and no candles return, the run is
skipped the same way.

> GitHub schedule caveat: `*/15` cron is best-effort — runs can be delayed a
> few minutes under load. The snapshot always covers the trailing 15-minute
> window from whenever it actually fires.
