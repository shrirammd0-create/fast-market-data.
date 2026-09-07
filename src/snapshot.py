"""REST snapshot mode — the serverless (cron) entry into the engine.

One cycle:

1. Pull full OANDA depth for gold concurrently (M1/M5/H4 bid-ask-mid
   candles, order book, position book) — see ``market_state``.
2. Rebuild the tick-rule footprint from the last ``lookback`` M1 bars
   (plus the COMEX GC true-volume footprint when a licensed feed is set).
3. Compute POC/VA/HVN/LVN, CVD + divergence, session VWAP,
   absorption/exhaustion, cross-asset correlations.
4. Write the machine-readable payload to ``data/market_state_snapshot.json``
   and append a human-readable block to ``market_updates.txt``.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from config.settings import Settings

from .adapters import CMEComexAdapter, OandaAdapter
from .adapters.base import Candle
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
from .market_state import build_market_state
from .resilience import TokenBucket
from .storage import JsonCache, append_block, write_json

logger = logging.getLogger(__name__)


def _fetch_closes(adapter: OandaAdapter, instrument: str, count: int) -> list[float]:
    """Best-effort close series for a correlation leg; [] on failure."""
    try:
        return [c.close for c in adapter.fetch_candles(instrument, "M1", count)]
    except Exception as exc:  # one bad leg must not kill the snapshot
        logger.warning("could not fetch %s for correlations: %s", instrument, exc)
        return []


def _fetch_closes_many(adapter: OandaAdapter, instruments: list[str], count: int) -> dict[str, list[float]]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {inst: pool.submit(_fetch_closes, adapter, inst, count) for inst in instruments}
        return {inst: fut.result() for inst, fut in futures.items()}


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


def profile_rows(engine: FootprintEngine, profile: VolumeProfile) -> dict:
    """Footprint levels as flat rows for the JSON payload."""
    return {
        "tick_size": engine.tick_size,
        "poc": profile.poc,
        "poc_volume": profile.poc_volume,
        "value_area_low": profile.value_area_low,
        "value_area_high": profile.value_area_high,
        "value_area_pct": profile.value_area_pct,
        "hvns": list(profile.hvns),
        "lvns": list(profile.lvns),
        "total_volume": profile.total_volume,
        "cumulative_delta": engine.cumulative_delta,
        "levels": [
            {
                "price": lv.price,
                "buy_volume": lv.buy_volume,
                "sell_volume": lv.sell_volume,
                "unknown_volume": lv.unknown_volume,
                "total_volume": lv.total_volume,
                "delta": lv.delta,
            }
            for lv in engine.levels()
        ],
        "bar_deltas": [
            {"close": b.close, "volume": b.volume, "delta": b.delta} for b in engine.bar_deltas
        ],
    }


def _depth_lines(payload: dict) -> list[str]:
    """Human-readable one-liners summarizing the machine payload."""
    lines: list[str] = []
    an = payload.get("analytics", {})

    svi = an.get("spread_volatility_index")
    if svi and svi.get("count"):
        voids = svi["liquidity_voids"]
        void_txt = (
            f"{len(voids)} void(s)"
            + (f", latest {voids[-1]['price_low']:.2f}–{voids[-1]['price_high']:.2f}"
               f" @ {voids[-1]['time'][11:16]}Z" if voids else "")
        )
        lines.append(f"Spread SVI:   mean {svi['mean']:.3f} | latest {svi['latest']:.3f} | "
                     f"max {svi['max']:.3f} | {void_txt}")

    ofi = an.get("order_flow_imbalance")
    if ofi:
        s = ofi["summary"]
        top = sorted(ofi["flagged_buckets"], key=lambda r: -r["total_pct"])[:3]
        top_txt = ", ".join(f"{r['price']:.2f}({r['dominant_side'][0].upper()})" for r in top) or "none"
        lines.append(f"Order book:   {s['flagged_count']}/{s['bucket_count']} buckets ≥{ofi['ratio_threshold']:.0f}:1 "
                     f"| long above {s['long_pct_above_price']:.2f}% short below {s['short_pct_below_price']:.2f}% "
                     f"| top: {top_txt}")

    pos = an.get("position_imbalance")
    if pos:
        tl, ts = pos["trapped_longs"][:2], pos["trapped_shorts"][:2]
        tl_txt = ", ".join(f"{r['price']:.2f}" for r in tl) or "none"
        ts_txt = ", ".join(f"{r['price']:.2f}" for r in ts) or "none"
        lines.append(f"Positions:    trapped longs {pos['trapped_long_pct_total']:.2f}% [{tl_txt}] "
                     f"| trapped shorts {pos['trapped_short_pct_total']:.2f}% [{ts_txt}]")

    vd = an.get("volume_delta_m5")
    if vd and vd.get("count"):
        lines.append(f"M5 delta:     tick-rule {vd['cumulative_delta']:+.0f} | "
                     f"body-weighted {vd['cumulative_delta_body_weighted']:+.0f} "
                     f"over {vd['count']} bars ({vd['total_volume']:.0f} ticks)")

    counts = " ".join(f"{g}:{len(rows)}" for g, rows in payload.get("candles", {}).items())
    ob = payload.get("order_book") or {}
    pb = payload.get("position_book") or {}
    lines.append(f"JSON:         candles[{counts}] OB:{len(ob.get('buckets', []))} "
                 f"PB:{len(pb.get('buckets', []))} buckets"
                 + (f" | errors: {', '.join(payload['errors'])}" if payload.get("errors") else ""))
    return lines


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
    depth_lines: list[str] | None = None,
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
    if depth_lines:
        lines.append("-" * 72)
        lines.extend(depth_lines)
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

    # --- Full-depth pull (M1/M5/H4 BAM candles + order/position books) ---
    state = build_market_state(oanda, settings, lookback_minutes, now=now)
    if state.depth.errors:
        logger.warning("market depth sections with problems: %s", state.depth.errors)

    # One extra candle seeds the tick rule and the return series.
    count = lookback_minutes + 1
    candles = [c.to_candle() for c in state.m1[-count:]]
    if len(candles) < 2:
        m1_error = state.depth.errors.get("candles:M1")
        if m1_error and m1_error != "empty response":
            logger.error("gold M1 candle fetch failed: %s", m1_error)
            return 1
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
    gc_profile_rows = None
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
                gc_profile_rows = profile_rows(gc_engine, gc_profile)
                gc_summary = (
                    f"COMEX GC:     POC {_fmt(gc_profile.poc)} | "
                    f"delta {gc_engine.cumulative_delta:+.0f} on "
                    f"{gc_engine.total_volume:.0f} contracts "
                    f"({gc_engine.trade_count} trades, exchange aggressor flags)"
                )
        except Exception as exc:
            logger.warning("COMEX GC fetch failed, continuing with spot only: %s", exc)

    # --- Cross-asset correlations: DXY proxy + major indices ---
    legs = list(DXY_WEIGHTS) + list(settings.index_instruments)
    series = _fetch_closes_many(oanda, legs, count)
    dxy = dxy_proxy_series({pair: series.get(pair, []) for pair in DXY_WEIGHTS})
    others: dict[str, list[float]] = {}
    if dxy:
        others["DXY(proxy)"] = dxy
    for idx in settings.index_instruments:
        if series.get(idx):
            others[idx] = series[idx]
    correlations = correlation_snapshot(closes, others)

    # --- Run-over-run change via local cache (falls back to the text log) ---
    cache = JsonCache(settings.cache_dir)
    prev_close = cache.get("last_close", max_age_seconds=6 * 3600)
    if prev_close is None:
        prev_close = _last_price_from_log(settings.output_file)
    cache.set("last_close", candles[-1].close)

    # --- Machine-readable payload: add the footprint + session context ---
    payload = state.payload
    payload["analytics"]["volume_profile_m1"] = profile_rows(engine, profile)
    if gc_profile_rows:
        payload["analytics"]["volume_profile_comex_gc"] = gc_profile_rows
    payload["analytics"]["session_context"] = {
        "session": session_name,
        "session_vwap": vwap.value,
        "price_vs_vwap": (
            None if vwap.value is None else ("above" if candles[-1].close >= vwap.value else "below")
        ),
        "cvd_lookback": engine.cumulative_delta,
        "divergences": [{"kind": d.kind, "description": d.description} for d in divergences],
        "absorption_events": [
            {"kind": e.kind, "price": e.price, "volume": e.volume, "description": e.description}
            for e in absorption_events
        ],
        "correlations_1m_log_returns": correlations,
        "previous_run_close": prev_close,
    }

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
        depth_lines=_depth_lines(payload),
    )
    print(block)
    if not no_append:
        path = append_block(settings.output_file, block)
        logger.info("appended snapshot block to %s", path)
        json_path = write_json(settings.json_output, payload)
        logger.info("wrote machine-readable market state to %s", json_path)
    return 0
