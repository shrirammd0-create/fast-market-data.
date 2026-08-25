#!/usr/bin/env python3
"""Daily gold macro tracker.

Fetches CME Gold Futures Volume / Open Interest and GLD ETF tonnage data,
analyzes them with Claude Sonnet, and writes LATEST_DAILY_REPORT.md.

Design rule: "Fetch No Matter What" — every network call is wrapped so a
failure falls through to the next source, then to the last-known cache,
then to a structured fallback notice. This script NEVER exits non-zero
and ALWAYS writes the report file.
"""

import csv
import io
import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "LATEST_DAILY_REPORT.md"
README_PATH = REPO_ROOT / "README.md"
CACHE_PATH = REPO_ROOT / "data" / "cache_daily.json"

README_START = "<!-- DAILY_REPORT:START -->"
README_END = "<!-- DAILY_REPORT:END -->"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.cmegroup.com/",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 25

# CME product id 437 = COMEX Gold futures (GC)
CME_QUOTES_URL = "https://www.cmegroup.com/CmeWS/mvc/Quotes/Future/437/G"
CME_VOI_URL = "https://www.cmegroup.com/CmeWS/mvc/Volume/Details/F/437/{date}/P"

# SPDR Gold Shares official public archive CSV (holdings incl. tonnes)
GLD_CSV_URL = "https://www.spdrgoldshares.com/assets/dynamic/GLD/GLD_US_archive_EN.csv"
GLD_PAGE_URL = "https://www.spdrgoldshares.com/usa/"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_get(url: str, timeout: int = REQUEST_TIMEOUT, headers: dict | None = None,
             params: dict | None = None):
    """GET a URL with browser headers. Returns a Response or None — never raises."""
    try:
        resp = requests.get(url, headers=headers or BROWSER_HEADERS,
                            params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp
        print(f"[warn] {url} returned HTTP {resp.status_code}")
    except Exception as exc:
        print(f"[warn] request to {url} failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# CME Volume / Open Interest
# ---------------------------------------------------------------------------

def fetch_cme_voi_details() -> dict | None:
    """Primary: CME's public volume/open-interest details endpoint for GC futures."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    # Try the last few weekdays — the endpoint lags by a session.
    for delta in range(1, 6):
        d = today - timedelta(days=delta)
        if d.weekday() >= 5:
            continue
        url = CME_VOI_URL.format(date=d.strftime("%Y%m%d"))
        resp = safe_get(url)
        if resp is None:
            continue
        try:
            data = resp.json()
            total = None
            if isinstance(data, dict):
                total = data.get("totals") or data.get("total")
                items = data.get("monthData") or data.get("items") or []
            else:
                items = []
            result = {
                "source": "CME Group volume/OI details endpoint (GC futures)",
                "trade_date": d.isoformat(),
            }
            if isinstance(total, dict):
                result["total_volume"] = total.get("totalVolume") or total.get("volume")
                result["open_interest"] = (total.get("atClose")
                                           or total.get("openInterest"))
                result["open_interest_change"] = total.get("change")
            if items:
                result["top_contracts"] = [
                    {
                        "month": it.get("month") or it.get("monthID"),
                        "volume": it.get("totalVolume") or it.get("volume"),
                        "open_interest": it.get("atClose") or it.get("openInterest"),
                    }
                    for it in items[:4]
                    if isinstance(it, dict)
                ]
            if result.get("total_volume") or result.get("top_contracts"):
                return result
        except Exception as exc:
            print(f"[warn] CME VOI parse failed for {d}: {exc}")
    return None


def fetch_cme_quotes() -> dict | None:
    """Secondary: CME public quotes endpoint — carries volume per contract."""
    resp = safe_get(CME_QUOTES_URL)
    if resp is None:
        return None
    try:
        data = resp.json()
        quotes = data.get("quotes") or []
        contracts = []
        for q in quotes[:4]:
            if not isinstance(q, dict):
                continue
            contracts.append({
                "contract": q.get("expirationMonth") or q.get("code"),
                "last": q.get("last"),
                "change": q.get("change"),
                "volume": q.get("volume"),
                "open_interest": q.get("openInterest"),
            })
        if contracts:
            return {"source": "CME Group quotes endpoint (GC futures)",
                    "contracts": contracts}
    except Exception as exc:
        print(f"[warn] CME quotes parse failed: {exc}")
    return None


def fetch_futures_volume_yfinance() -> dict | None:
    """Tertiary fallback: GC=F daily volume from Yahoo via yfinance (no OI available)."""
    try:
        import yfinance as yf
        hist = yf.Ticker("GC=F").history(period="10d", interval="1d")
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        avg_vol = float(hist["Volume"].mean())
        return {
            "source": "yfinance (GC=F daily bars — volume only, no open interest)",
            "trade_date": str(hist.index[-1].date()),
            "close": round(float(last["Close"]), 2),
            "daily_volume": int(last["Volume"]),
            "10d_avg_volume": int(avg_vol),
            "note": "Open interest is not available from this fallback source.",
        }
    except Exception as exc:
        print(f"[warn] yfinance GC=F fallback failed: {exc}")
        return None


def fetch_cme_data() -> dict | None:
    return fetch_cme_voi_details() or fetch_cme_quotes() or fetch_futures_volume_yfinance()


# ---------------------------------------------------------------------------
# GLD ETF tonnage
# ---------------------------------------------------------------------------

def _parse_float(value: str) -> float | None:
    try:
        return float(value.replace(",", "").strip())
    except Exception:
        return None


def fetch_gld_tonnage_csv() -> dict | None:
    """Primary: official SPDR Gold Shares archive CSV (contains tonnes in trust)."""
    resp = safe_get(GLD_CSV_URL)
    if resp is None:
        return None
    try:
        rows = list(csv.reader(io.StringIO(resp.text)))
        header_idx, tonnes_col, date_col = None, None, 0
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                if "tonne" in cell.lower():
                    header_idx, tonnes_col = i, j
                    break
            if header_idx is not None:
                break
        if header_idx is None:
            return None
        data_rows = [r for r in rows[header_idx + 1:]
                     if len(r) > tonnes_col and _parse_float(r[tonnes_col]) is not None]
        if not data_rows:
            return None
        latest, prev = data_rows[-1], (data_rows[-2] if len(data_rows) > 1 else None)
        latest_t = _parse_float(latest[tonnes_col])
        prev_t = _parse_float(prev[tonnes_col]) if prev else None
        return {
            "source": "SPDR Gold Shares official archive CSV",
            "as_of": latest[date_col].strip() if latest else None,
            "tonnes_in_trust": latest_t,
            "previous_tonnes": prev_t,
            "daily_change_tonnes": (round(latest_t - prev_t, 2)
                                    if latest_t is not None and prev_t is not None
                                    else None),
        }
    except Exception as exc:
        print(f"[warn] GLD CSV parse failed: {exc}")
        return None


def fetch_gld_tonnage_page() -> dict | None:
    """Secondary: scrape the SPDR Gold Shares USA page for a tonnes figure."""
    resp = safe_get(GLD_PAGE_URL)
    if resp is None:
        return None
    try:
        import re
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        m = re.search(r"([\d,]+\.\d+)\s*[Tt]onnes", text)
        if m:
            return {
                "source": "spdrgoldshares.com page scrape",
                "tonnes_in_trust": _parse_float(m.group(1)),
            }
    except Exception as exc:
        print(f"[warn] GLD page scrape failed: {exc}")
    return None


def fetch_gld_yfinance() -> dict | None:
    """Tertiary fallback: GLD ETF market stats via yfinance (volume/price proxy)."""
    try:
        import yfinance as yf
        t = yf.Ticker("GLD")
        hist = t.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        return {
            "source": "yfinance (GLD ETF — market proxy, no tonnage)",
            "as_of": str(hist.index[-1].date()),
            "close": round(float(last["Close"]), 2),
            "daily_volume": int(last["Volume"]),
            "note": "Tonnage unavailable from this fallback; ETF price/volume only.",
        }
    except Exception as exc:
        print(f"[warn] yfinance GLD fallback failed: {exc}")
        return None


def fetch_gld_data() -> dict | None:
    return fetch_gld_tonnage_csv() or fetch_gld_tonnage_page() or fetch_gld_yfinance()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception as exc:
        print(f"[warn] cache load failed: {exc}")
    return {}


def save_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        print(f"[warn] cache save failed: {exc}")


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(payload: dict) -> str | None:
    """Ask Claude Sonnet for a daily macro read. Returns None on any failure."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=3000,
            system=(
                "You are a gold-market macro analyst writing a once-daily report. "
                "You receive raw JSON with CME gold futures volume/open-interest "
                "data and GLD ETF holdings (tonnage) data. Write GitHub-flavored "
                "markdown with exactly these sections: '### Macro Read' (a short "
                "paragraph on what volume/OI and ETF flows imply about positioning "
                "and conviction), '### Institutional Flow Signal' "
                "(Accumulation/Distribution/Neutral with rationale), and "
                "'### What To Watch Tomorrow' (up to 3 bullets). Do not invent "
                "numbers that are not in the JSON; if a field is missing or came "
                "from a degraded fallback source, note that plainly. No preamble, "
                "no disclaimers."
            ),
            messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
    except Exception as exc:
        print(f"[warn] Claude analysis failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def build_report(cme: dict | None, gld: dict | None, analysis: str | None,
                 cache: dict) -> str:
    lines = [
        "# 📅 Gold — Daily Macro Report",
        "",
        f"**Last updated:** {utc_now()}",
        "",
        "## 🏦 CME Gold Futures — Volume & Open Interest",
        "",
    ]

    if cme:
        lines += [f"- **Source:** {cme.get('source', 'n/a')}"]
        if cme.get("trade_date"):
            lines += [f"- **Trade date:** {cme['trade_date']}"]
        if cme.get("total_volume") is not None:
            lines += [f"- **Total volume:** {cme['total_volume']:,}"
                      if isinstance(cme["total_volume"], int)
                      else f"- **Total volume:** {cme['total_volume']}"]
        if cme.get("open_interest") is not None:
            lines += [f"- **Open interest:** {cme['open_interest']}"]
        if cme.get("open_interest_change") is not None:
            lines += [f"- **OI change:** {cme['open_interest_change']}"]
        if cme.get("daily_volume") is not None:
            lines += [f"- **Daily volume:** {cme['daily_volume']:,} "
                      f"(10-day avg: {cme.get('10d_avg_volume', 'n/a'):,})"
                      if isinstance(cme.get("10d_avg_volume"), int)
                      else f"- **Daily volume:** {cme['daily_volume']:,}"]
        if cme.get("close") is not None:
            lines += [f"- **Close:** {cme['close']}"]
        if cme.get("contracts"):
            lines += ["", "| Contract | Last | Change | Volume | Open Interest |",
                      "|---|---|---|---|---|"]
            for c in cme["contracts"]:
                lines.append(
                    f"| {c.get('contract', 'n/a')} | {c.get('last', 'n/a')} | "
                    f"{c.get('change', 'n/a')} | {c.get('volume', 'n/a')} | "
                    f"{c.get('open_interest', 'n/a')} |"
                )
        if cme.get("top_contracts"):
            lines += ["", "| Contract | Volume | Open Interest |", "|---|---|---|"]
            for c in cme["top_contracts"]:
                lines.append(f"| {c.get('month', 'n/a')} | {c.get('volume', 'n/a')} | "
                             f"{c.get('open_interest', 'n/a')} |")
        if cme.get("note"):
            lines += ["", f"> ℹ️ {cme['note']}"]
        lines += [""]
    elif cache.get("cme"):
        lines += [
            f"> ⚠️ Live CME sources unavailable — showing cached data from "
            f"{cache.get('updated_at', 'unknown time')}:",
            "",
            "```json",
            json.dumps(cache["cme"], indent=2),
            "```",
            "",
        ]
    else:
        lines += [
            "> ⚠️ **Fallback state:** all CME volume/OI sources (and the cache) were "
            "unavailable this run. The next scheduled run will retry automatically.",
            "",
        ]

    lines += ["## 🪙 GLD ETF Holdings (Tonnage)", ""]
    if gld:
        lines += [f"- **Source:** {gld.get('source', 'n/a')}"]
        if gld.get("as_of"):
            lines += [f"- **As of:** {gld['as_of']}"]
        if gld.get("tonnes_in_trust") is not None:
            lines += [f"- **Tonnes in trust:** **{gld['tonnes_in_trust']}**"]
        if gld.get("daily_change_tonnes") is not None:
            arrow = "🔺" if gld["daily_change_tonnes"] > 0 else (
                "🔻" if gld["daily_change_tonnes"] < 0 else "▪️")
            lines += [f"- **Daily change:** {arrow} {gld['daily_change_tonnes']} tonnes"]
        if gld.get("close") is not None:
            lines += [f"- **GLD close:** {gld['close']}"]
        if gld.get("daily_volume") is not None:
            lines += [f"- **GLD volume:** {gld['daily_volume']:,}"]
        if gld.get("note"):
            lines += [f"> ℹ️ {gld['note']}"]
        lines += [""]
    elif cache.get("gld"):
        lines += [
            f"> ⚠️ Live GLD sources unavailable — showing cached data from "
            f"{cache.get('updated_at', 'unknown time')}:",
            "",
            "```json",
            json.dumps(cache["gld"], indent=2),
            "```",
            "",
        ]
    else:
        lines += [
            "> ⚠️ **Fallback state:** all GLD tonnage sources (and the cache) were "
            "unavailable this run. The next scheduled run will retry automatically.",
            "",
        ]

    lines += ["## 🤖 AI Analysis (Claude Sonnet)", ""]
    if analysis:
        lines += [analysis, ""]
    else:
        lines += [
            "> ⚠️ AI analysis was unavailable this run (API error or missing "
            "`ANTHROPIC_API_KEY`). Raw data above is still current.",
            "",
        ]

    lines += [
        "---",
        "*Auto-generated daily by "
        "[`scripts/daily_macro.py`](scripts/daily_macro.py). "
        "Not investment advice.*",
        "",
    ]
    return "\n".join(lines)


def update_readme_section(report_md: str) -> None:
    """Splice the report into README.md between the DAILY markers, if present."""
    try:
        if not README_PATH.exists():
            return
        readme = README_PATH.read_text()
        if README_START not in readme or README_END not in readme:
            return
        pre, rest = readme.split(README_START, 1)
        _, post = rest.split(README_END, 1)
        README_PATH.write_text(
            pre + README_START + "\n" + report_md.strip() + "\n" + README_END + post
        )
    except Exception as exc:
        print(f"[warn] README update failed: {exc}")


def main() -> None:
    print(f"[info] daily macro run starting at {utc_now()}")
    cache = load_cache()

    cme = fetch_cme_data()
    gld = fetch_gld_data()

    analysis = None
    if cme or gld:
        analysis = analyze_with_claude({
            "timestamp_utc": utc_now(),
            "cme_volume_open_interest": cme,
            "gld_etf_holdings": gld,
            "note": "Some fields may be null if a source was unavailable.",
        })

    report = build_report(cme, gld, analysis, cache)
    REPORT_PATH.write_text(report)
    print(f"[info] wrote {REPORT_PATH}")

    update_readme_section(report)

    new_cache = {
        "updated_at": utc_now(),
        "cme": cme or cache.get("cme"),
        "gld": gld or cache.get("gld"),
    }
    save_cache(new_cache)
    print("[info] run complete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        try:
            REPORT_PATH.write_text(
                "# 📅 Gold — Daily Macro Report\n\n"
                f"**Last attempted update:** {utc_now()}\n\n"
                "> ⚠️ **Fallback state:** an unexpected error occurred this run. "
                "The previous report content was replaced with this notice; the "
                "next scheduled run will retry automatically.\n"
            )
        except Exception:
            pass
    sys.exit(0)
