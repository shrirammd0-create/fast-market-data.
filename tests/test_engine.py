from datetime import datetime, timezone

from src.adapters.base import Candle
from src.engine.aggressor import Side, TickRuleClassifier, classify_by_flag
from src.engine.footprint import FootprintEngine
from src.engine.volume_nodes import build_profile


def ts(minute):
    return datetime(2026, 9, 7, 3, minute, tzinfo=timezone.utc)


# --- aggressor classification ---

def test_flag_classification_covers_exchange_conventions():
    assert classify_by_flag("B") is Side.BUY
    assert classify_by_flag("buy") is Side.BUY
    assert classify_by_flag("A") is Side.BUY      # matched at the ask
    assert classify_by_flag("1") is Side.BUY      # CME numeric
    assert classify_by_flag("S") is Side.SELL
    assert classify_by_flag("BID") is Side.SELL
    assert classify_by_flag("2") is Side.SELL
    assert classify_by_flag(None) is Side.UNKNOWN
    assert classify_by_flag("??") is Side.UNKNOWN


def test_tick_rule_uptick_downtick_and_zero_tick():
    clf = TickRuleClassifier(seed_price=100.0)
    assert clf.classify(100.1) is Side.BUY    # uptick
    assert clf.classify(100.1) is Side.BUY    # zero tick inherits
    assert clf.classify(100.0) is Side.SELL   # downtick
    assert clf.classify(100.0) is Side.SELL   # zero tick inherits
    assert clf.classify(100.2) is Side.BUY


def test_tick_rule_first_print_without_seed_is_unknown():
    clf = TickRuleClassifier()
    assert clf.classify(100.0) is Side.UNKNOWN
    assert clf.classify(100.5) is Side.BUY


# --- footprint aggregation ---

def test_footprint_levels_track_buy_sell_and_delta():
    fp = FootprintEngine(tick_size=0.1)
    fp.add_trade(3646.10, 5, Side.BUY)
    fp.add_trade(3646.10, 2, Side.SELL)
    fp.add_trade(3646.12, 3, Side.BUY)  # quantizes onto the 3646.1 level
    fp.add_trade(3646.30, 4, Side.SELL)

    levels = fp.levels()
    assert [lv.price for lv in levels] == [3646.1, 3646.3]
    top = levels[0]
    assert top.buy_volume == 8 and top.sell_volume == 2
    assert top.total_volume == 10 and top.delta == 6
    assert fp.cumulative_delta == 8 - 6  # +8 buys, -6 sells overall
    assert fp.total_volume == 14


def test_footprint_ingest_candles_uses_tick_rule():
    fp = FootprintEngine(tick_size=0.1)
    candles = [
        Candle(ts(0), 100.0, 100.2, 99.9, 100.1, 50),   # up vs open -> BUY
        Candle(ts(1), 100.1, 100.3, 100.0, 100.2, 30),  # uptick -> BUY
        Candle(ts(2), 100.2, 100.2, 99.8, 99.9, 40),    # downtick -> SELL
    ]
    fp.ingest_candles(candles)
    assert fp.cumulative_delta == 50 + 30 - 40
    assert [round(b.delta) for b in fp.bar_deltas] == [50, 30, -40]


# --- volume nodes ---

def make_profile_engine():
    fp = FootprintEngine(tick_size=1.0)
    # price -> volume: 100:5, 101:10, 102:40 (POC), 103:20, 104:2
    for price, vol in [(100, 5), (101, 10), (102, 40), (103, 20), (104, 2)]:
        fp.add_trade(price, vol, Side.BUY)
    return fp


def test_poc_and_value_area():
    profile = build_profile(make_profile_engine(), value_area_pct=0.70)
    assert profile.poc == 102
    assert profile.poc_volume == 40
    # total=77; expansion from POC: +103 (60) -> under 53.9? 70% of 77 = 53.9,
    # 40 < 53.9 so annex 103 (20) -> 60 >= 53.9. VA = [102, 103].
    assert profile.value_area_low == 102
    assert profile.value_area_high == 103


def test_hvn_lvn_detection():
    profile = build_profile(make_profile_engine(), hvn_ratio=1.5, lvn_ratio=0.5)
    mean = 77 / 5  # 15.4
    assert 102 in profile.hvns            # 40 >= 23.1
    assert 104 in profile.lvns and 100 in profile.lvns  # <= 7.7
    assert 102 not in profile.lvns


def test_empty_profile_is_safe():
    profile = build_profile(FootprintEngine(tick_size=0.1))
    assert profile.poc is None
    assert profile.total_volume == 0
