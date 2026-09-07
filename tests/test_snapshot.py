"""End-to-end snapshot run against a mocked OANDA REST backend."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from config.settings import Settings
from src.snapshot import _last_price_from_log, run_snapshot


def oanda_payload(start, n=16, base=3640.0, step=0.5, volume=100):
    candles = []
    for i in range(n):
        o = base + i * step
        c = o + step
        candles.append({
            "time": (start + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
            "volume": volume,
            "complete": True,
            "mid": {"o": f"{o:.1f}", "h": f"{c + 0.2:.1f}",
                    "l": f"{o - 0.2:.1f}", "c": f"{c:.1f}"},
        })
    return {"candles": candles}


def make_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.json.return_value = payload
    return resp


def test_snapshot_appends_block(tmp_path):
    start = datetime(2026, 9, 7, 3, 0, tzinfo=timezone.utc)
    settings = Settings(
        oanda_api_key="test-key",
        output_file=str(tmp_path / "market_updates.txt"),
        json_output=str(tmp_path / "data" / "market_state_snapshot.json"),
        cache_dir=str(tmp_path / ".cache"),
        requests_per_minute=6000,
    )

    with patch("requests.Session.get", return_value=make_response(oanda_payload(start))):
        code = run_snapshot(settings, lookback_minutes=15)

    assert code == 0
    content = (tmp_path / "market_updates.txt").read_text()
    assert "GOLD MICROSTRUCTURE SNAPSHOT" in content
    assert "POC:" in content
    assert "CVD (15m):" in content
    assert "Session VWAP:" in content
    assert "Correlations" in content

    # Second run should recover the previous price and separate blocks.
    with patch("requests.Session.get", return_value=make_response(oanda_payload(start))):
        assert run_snapshot(settings, lookback_minutes=15) == 0
    content = (tmp_path / "market_updates.txt").read_text()
    assert content.count("GOLD MICROSTRUCTURE SNAPSHOT") == 2
    assert "vs last run" in content


def test_snapshot_without_api_key_exits_cleanly(tmp_path):
    settings = Settings(oanda_api_key=None,
                        output_file=str(tmp_path / "out.txt"),
                        cache_dir=str(tmp_path / ".cache"))
    assert run_snapshot(settings) == 0
    assert not (tmp_path / "out.txt").exists()


def test_last_price_from_log(tmp_path):
    log = tmp_path / "log.txt"
    log.write_text("header\nPrice:        3646.12  (x)\nfooter\nPrice:        3650.00\n")
    assert _last_price_from_log(str(log)) == 3650.00
    assert _last_price_from_log(str(tmp_path / "missing.txt")) is None
