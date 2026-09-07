import argparse

import pytest

from main import build_parser, parse_lookback


def test_parse_lookback_formats():
    assert parse_lookback("15m") == 15
    assert parse_lookback("2h") == 120
    assert parse_lookback("45") == 45


def test_parse_lookback_rejects_garbage():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_lookback("soon")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_lookback("0m")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_lookback("48h")


def test_default_invocation_is_snapshot_15m():
    args = build_parser().parse_args([])
    assert args.mode == "snapshot"
    assert args.lookback == 15


def test_snapshot_mode_flags():
    args = build_parser().parse_args(
        ["--mode", "snapshot", "--lookback", "15m", "--no-append"]
    )
    assert args.lookback == 15
    assert args.no_append
