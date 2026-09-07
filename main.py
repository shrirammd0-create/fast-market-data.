#!/usr/bin/env python3
"""Entry point / CLI runner for the gold market microstructure engine.

Usage (snapshot mode, as run by the GitHub Actions cron):

    python main.py --mode snapshot --lookback 15m
"""

from __future__ import annotations

import argparse
import logging
import sys


def parse_lookback(raw: str) -> int:
    """'15m' -> 15, '2h' -> 120, bare '15' -> 15 (minutes)."""
    raw = raw.strip().lower()
    try:
        if raw.endswith("m"):
            minutes = int(raw[:-1])
        elif raw.endswith("h"):
            minutes = int(raw[:-1]) * 60
        else:
            minutes = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid lookback '{raw}' (use e.g. 15m, 1h)")
    if not 2 <= minutes <= 1440:
        raise argparse.ArgumentTypeError("lookback must be between 2m and 24h")
    return minutes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fast-market-data",
        description="Gold-focused market microstructure engine (footprint, CVD, VWAP, volume nodes).",
    )
    parser.add_argument(
        "--mode",
        choices=["snapshot"],
        default="snapshot",
        help="snapshot: fetch recent candles over REST, compute the footprint "
             "summary, append it to the output file, and exit (cron-friendly).",
    )
    parser.add_argument(
        "--lookback",
        type=parse_lookback,
        default=15,
        metavar="WINDOW",
        help="analysis window, e.g. 15m or 1h (default: 15m)",
    )
    parser.add_argument("--output", help="override output text file (default: market_updates.txt)")
    parser.add_argument("--json-output",
                        help="override machine-readable output (default: data/market_state_snapshot.json)")
    parser.add_argument("--no-append", action="store_true",
                        help="print the snapshot but do not write the text or JSON outputs")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    import dataclasses

    from config.settings import load_settings
    from src.snapshot import run_snapshot

    settings = load_settings()
    if args.output:
        settings = dataclasses.replace(settings, output_file=args.output)
    if args.json_output:
        settings = dataclasses.replace(settings, json_output=args.json_output)

    if args.mode == "snapshot":
        return run_snapshot(settings, lookback_minutes=args.lookback, no_append=args.no_append)
    return 2


if __name__ == "__main__":
    sys.exit(main())
