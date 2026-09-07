"""REST snapshot mode — the serverless (cron) entry into the engine.

Fetches the last N minutes of 1-minute candles over REST, rebuilds the
gold footprint (plus the COMEX GC true-volume footprint when a licensed
feed is configured), computes POC/VA/HVN/LVN, CVD + divergence, session
VWAP, absorption/exhaustion, and cross-asset correlations, appends a
timestamped block to the output text file, and exits cleanly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from config.settings import Settings

from .adapters import CMEComexAdapter, OandaAdapter
from .adapters.base import AdapterError, Candle
from .engine import FootprintEngine, TickRuleClassifier, build_profile, classify_by_flag
from .engine.volume_nodes import VolumeProfile
from .indicators import (
    DXY_WEIGHTS,
    SessionVWAP,
    correlation_snapshot,
    detect_absorption_exhaustion,
    detect_divergence,
    dxy_proxy_series,
    session_for,
)
from .resilience import RetryableError, TokenBucket
from .storage import JsonCache, append_block

logger = logging.getLogger(__name__)


def _fetch_closes(adapter: OandaAdapter, instrument: str, count: int) -> list[float]:
    """Best-effort close series for a correlation leg; [] on failure."""
    try:
        return [c.close for c in adapter.fetch_candles(instrument, "M1", count)]
    except Exception as exc:  # one bad leg must not kill the snapshot
        logger.warning("could not fetch %s for correlations: %s", instrument, exc)
        return []


def _fmt(value: float | None, spec: str = ".2f", none: str = "n/a") -> str:
    return format(value, spec) if value is not None else none


def _last_price_from_log(path: str) -> float | None:
    """Recover the previous snapshot's price from the committed text log.

    The cron environment is a fresh checkout every run, so the local cache
    never survives — but market_updates.txt does, because it is committed.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            last = None
            for line in fh:
                if line.startswith("Price:"):
                    last = line.split()[1]
            return float(last) if last else None
    except (OSError, ValueError, IndexError):
        return None


def format_snapshot_block(
    *,
    now: datetime,
    settings: Settings,
    lookback_minutes: int,
    candles: list[Candle],
    profile: VolumeProfile,
    cvd_value: float,
    divergences,
    vwap_value: float | None,
    session_name: str,
    absorption_events,
    correlations: dict[str, float | None],
    prev_close: float | None,
    gc_summary: str | None,
) -> str:
    last = candles[-1]
    change_line = ""
    if prev_close:
        chg = last.close - prev_close
        change_line = f"  (vs last run: {chg:+.2f} / {chg / prev_close:+.3%})"

    lines = [
        "=" * 72,
        f"GOLD MICROSTRUCTURE SNAPSHOT — {now:%Y-%m-%d %H:%M:%S} UTC",
        f"{settings.gold_instrument} (OANDA spot, tick-rule volume) | lookback {lookback_minutes}m "
        f"({len(candles)} x M1)",
        "-" * 72,
        f"Price:        {last.close:.2f}{change_line}",
        f"Session VWAP: {_fmt(vwap_value)}  [{session_name}]"
        + (
            f"  price {'above' if last.close >= vwap_value else 'below'} VWAP"
            if vwap_value is not None else ""
        ),
        f"POC:          {_fmt(profile.poc)}  ({profile.poc_volume:.0f} vol)",
        f"Value Area:   {_fmt(profile.value_area_low)} – {_fmt(profile.value_area_high)}"
        f"  ({profile.value_area_pct:.0%} of {profile.total_volume:.0f} total vol)",
        f"HVN:          {', '.join(f'{p:.2f}' for p in profile.hvns) or 'none'}",
        f"LVN:          {', '.join(f'{p:.2f}' for p in profile.lvns) or 'none'}",
        f"CVD ({lookback_minutes}m):    {cvd_value:+.0f}",
    ]
    if divergences:
        for d in divergences:
            lines.append(f"DIVERGENCE:   [{d.kind.upper()}] {d.description}")
    else:
        lines.append("DIVERGENCE:   none flagged")
    if absorption_events:
        for e in absorption_events:
            lines.append(f"{e.kind.upper():<13} {e.description}")
    else:
        lines.append("ABSORPTION:   none flagged")
    if gc_summary:
        lines.append(gc_summary)
    corr_bits = [f"{name}: {_fmt(val, '+.2f')}" for name, val in correlations.items()]
    lines.append("Correlations (1m log-returns): " + (" | ".join(corr_bits) or "n/a"))
    lines.append("=" * 72)
    return "\n".join(lines)


def run_snapshot(settings: Settings, lookback_minutes: int = 15, no_append: bool = False) -> int:
    """Execute one snapshot cycle. Returns a process exit code."""
    now = datetime.now(timezone.utc)
    bucket = TokenBucket(rate_per_minute=settings.requests_per_minute)
    oanda = OandaAdapter(settings.oanda_api_key, settings.oanda_env, rate_limiter=bucket)

    if not oanda.is_configured():
        logger.warning(
            "OANDA_API_KEY is not set — nothing to fetch. Add it to .env locally "
            "or to the repository's GitHub Secrets for the cron workflow."
        )
        return 0

    # One extra candle seeds the tick rule and the return series.
    count = lookback_minutes + 1
    try:
        candles = oanda.fetch_candles(settings.gold_instrument, "M1", count)
    except AdapterError as exc:
        logger.error("gold candle fetch failed permanently: %s", exc)
        return 1
    except RetryableError as exc:
        # Retries exhausted (outage, blocked network): fail with a clean
        # log line, not a traceback.
        logger.error("gold candle fetch still failing after retries: %s", exc)
        return 1
    if len(candles) < 2:
        logger.warning("no recent candles for %s (market closed?) — skipping this run",
                       settings.gold_instrument)
        return 0

    # --- Footprint (decentralized tick-rule) ---
    engine = FootprintEngine(tick_size=settings.tick_size)
    classifier = TickRuleClassifier(seed_price=candles[0].close)
    engine.ingest_candles(candles[1:], classifier)
    profile = build_profile(engine)

    closes = [c.close for c in candles]
    bar_deltas = [b.delta for b in engine.bar_deltas]
    divergences = detect_divergence(closes[1:], bar_deltas)
    absorption_events = detect_absorption_exhaustion(candles[1:], bar_deltas)

    # --- Session VWAP over the lookback window ---
    vwap = SessionVWAP()
    for c in candles[1:]:
        typical = (c.high + c.low + c.close) / 3.0
        vwap.update(c.ts, typical, c.volume)
    session_name = vwap.session or session_for(now)

    # --- COMEX GC true-volume footprint (centralized), when configured ---
    gc_summary = None
    cme = CMEComexAdapter(settings.cme_api_url, settings.cme_api_key, rate_limiter=bucket)
    if cme.is_configured():
        try:
            gc_trades = cme.fetch_trades("GC", lookback_minutes)
            if gc_trades:
                gc_engine = FootprintEngine(tick_size=0.1)  # GC tick size $0.10
                gc_engine.ingest_trades(
                    (t.price, t.size, classify_by_flag(t.aggressor)) for t in gc_trades
                )
                gc_profile = build_profile(gc_engine)
                gc_summary = (
                    f"COMEX GC:     POC {_fmt(gc_profile.poc)} | "
                    f"delta {gc_engine.cumulative_delta:+.0f} on "
                    f"{gc_engine.total_volume:.0f} contracts "
                    f"({gc_engine.trade_count} trades, exchange aggressor flags)"
                )
        except Exception as exc:
            logger.warning("COMEX GC fetch failed, continuing with spot only: %s", exc)

    # --- Cross-asset correlations: DXY proxy + major indices ---
    components = {pair: _fetch_closes(oanda, pair, count) for pair in DXY_WEIGHTS}
    dxy = dxy_proxy_series(components)
    others: dict[str, list[float]] = {}
    if dxy:
        others["DXY(proxy)"] = dxy
    for idx in settings.index_instruments:
        series = _fetch_closes(oanda, idx, count)
        if series:
            others[idx] = series
    correlations = correlation_snapshot(closes, others)

    # --- Run-over-run change via local cache ---
    cache = JsonCache(settings.cache_dir)
    prev_close = cache.get("last_close", max_age_seconds=6 * 3600)
    if prev_close is None:
        prev_close = _last_price_from_log(settings.output_file)
    cache.set("last_close", candles[-1].close)

    block = format_snapshot_block(
        now=now,
        settings=settings,
        lookback_minutes=lookback_minutes,
        candles=candles[1:],
        profile=profile,
        cvd_value=engine.cumulative_delta,
        divergences=divergences,
        vwap_value=vwap.value,
        session_name=session_name,
        absorption_events=absorption_events,
        correlations=correlations,
        prev_close=prev_close,
        gc_summary=gc_summary,
    )
    print(block)
    if not no_append:
        path = append_block(settings.output_file, block)
        logger.info("appended snapshot block to %s", path)
    return 0
