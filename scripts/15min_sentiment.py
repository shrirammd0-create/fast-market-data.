#!/usr/bin/env python3
"""15-minute gold market sentiment tracker.

Fetches intraday gold price data and broker/retail sentiment signals,
analyzes them with Claude Haiku, and writes LATEST_15MIN_REPORT.md.

Design rule: "Fetch No Matter What" — every network call is wrapped so a
failure falls through to the next source, then to the last-known cache,
then to a structured fallback notice. This script NEVER exits non-zero
and ALWAYS writes the report file.
"""

import json
import os
import re
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "LATEST_15MIN_REPORT.md"
README_PATH = REPO_ROOT / "README.md"
CACHE_PATH = REPO_ROOT / "data" / "cache_15min.json"

README_START = "<!-- 15MIN_REPORT:START -->"
README_END = "<!-- 15MIN_REPORT:END -->"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

REQUEST_TIMEOUT = 20


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
# Price data (primary: yfinance, fallback: raw Yahoo chart API)
# ---------------------------------------------------------------------------

def fetch_price_yfinance() -> dict | None:
    """Intraday price via the yfinance library. Tries gold futures, spot proxy, GLD."""
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"[warn] yfinance import failed: {exc}")
        return None

    for symbol, label in (("GC=F", "COMEX Gold Futures"),
                          ("XAUUSD=X", "XAU/USD Spot Proxy"),
                          ("GLD", "SPDR Gold Shares ETF")):
        try:
            hist = yf.Ticker(symbol).history(period="1d", interval="15m")
            if hist is None or hist.empty:
                continue
            last = hist.iloc[-1]
            first = hist.iloc[0]
            change = float(last["Close"]) - float(first["Open"])
            pct = (change / float(first["Open"])) * 100 if float(first["Open"]) else 0.0
            return {
                "source": f"yfinance ({label}, {symbol})",
                "symbol": symbol,
                "last_price": round(float(last["Close"]), 2),
                "session_open": round(float(first["Open"]), 2),
                "session_high": round(float(hist["High"].max()), 2),
                "session_low": round(float(hist["Low"].min()), 2),
                "session_change": round(change, 2),
                "session_change_pct": round(pct, 2),
                "last_bar_volume": int(last.get("Volume", 0) or 0),
                "bars": len(hist),
            }
        except Exception as exc:
            print(f"[warn] yfinance {symbol} failed: {exc}")
    return None


def fetch_price_yahoo_api() -> dict | None:
    """Fallback: raw Yahoo Finance chart API via requests."""
    for symbol in ("GC=F", "XAUUSD=X", "GLD"):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        resp = safe_get(url, params={"interval": "15m", "range": "1d"})
        if resp is None:
            continue
        try:
            result = resp.json()["chart"]["result"][0]
            meta = result["meta"]
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price is None:
                continue
            change = (price - prev) if prev else None
            return {
                "source": f"Yahoo Finance chart API ({symbol})",
                "symbol": symbol,
                "last_price": round(float(price), 2),
                "previous_close": round(float(prev), 2) if prev else None,
                "session_change": round(float(change), 2) if change is not None else None,
                "session_change_pct": round(change / prev * 100, 2) if change is not None and prev else None,
            }
        except Exception as exc:
            print(f"[warn] parsing Yahoo chart API for {symbol} failed: {exc}")
    return None


def fetch_price() -> dict | None:
    return fetch_price_yfinance() or fetch_price_yahoo_api()


# ---------------------------------------------------------------------------
# Retail / broker sentiment (Myfxbook outlook scrape, RSS news headlines)
# ---------------------------------------------------------------------------

def fetch_myfxbook_sentiment() -> dict | None:
    """Scrape the public Myfxbook community outlook page for XAU/USD positioning."""
    resp = safe_get("https://www.myfxbook.com/community/outlook/XAUUSD")
    if resp is None:
        return None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        # Look for "Short XX%" / "Long XX%" style percentages near the symbol table.
        shorts = re.search(r"Short[^0-9]{0,20}(\d{1,3})\s*%", text)
        longs = re.search(r"Long[^0-9]{0,20}(\d{1,3})\s*%", text)
        if shorts and longs:
            return {
                "source": "Myfxbook community outlook (XAU/USD)",
                "retail_long_pct": int(longs.group(1)),
                "retail_short_pct": int(shorts.group(1)),
            }
    except Exception as exc:
        print(f"[warn] Myfxbook parse failed: {exc}")
    return None


RSS_FEEDS = [
    ("Google News (gold price)",
     "https://news.google.com/rss/search?q=gold+price+XAUUSD&hl=en-US&gl=US&ceid=US:en"),
    ("Kitco News", "https://www.kitco.com/rss/category/commentaries.xml"),
    ("Reddit r/Gold", "https://www.reddit.com/r/Gold/.rss"),
]


def _parse_rss_titles(xml_text: str, limit: int = 8) -> list[str]:
    titles: list[str] = []
    try:
        root = ET.fromstring(xml_text)
        # RSS 2.0: channel/item/title; Atom: {ns}entry/{ns}title
        for item in root.iter():
            tag = item.tag.rsplit("}", 1)[-1]
            if tag in ("item", "entry"):
                for child in item:
                    if child.tag.rsplit("}", 1)[-1] == "title" and child.text:
                        titles.append(child.text.strip())
                        break
            if len(titles) >= limit:
                break
    except Exception as exc:
        print(f"[warn] RSS parse failed: {exc}")
        # Last-ditch regex extraction of <title> tags
        titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                            xml_text)[1:limit + 1]
    return [t for t in titles if t][:limit]


def fetch_news_headlines() -> dict | None:
    """Collect recent gold-related headlines from public RSS feeds."""
    collected: dict[str, list[str]] = {}
    for name, url in RSS_FEEDS:
        resp = safe_get(url)
        if resp is None:
            continue
        titles = _parse_rss_titles(resp.text)
        if titles:
            collected[name] = titles
    if collected:
        return {"source": "Public RSS feeds", "headlines": collected}
    return None


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
    """Ask Claude Haiku for a short intraday sentiment read. Returns None on any failure."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=(
                "You are a concise gold-market analyst writing a 15-minute intraday "
                "update. You receive raw JSON with intraday price data, retail "
                "positioning, and recent headlines. Write GitHub-flavored markdown "
                "with exactly these sections: '### Intraday Read' (2-3 sentences), "
                "'### Sentiment Signal' (Bullish/Bearish/Neutral with one-line "
                "rationale), and '### Key Headlines Driving Sentiment' (up to 3 "
                "bullets). Do not invent data that is not in the JSON; if a field "
                "is missing, say so briefly. No preamble, no disclaimers."
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

def build_report(price: dict | None, sentiment: dict | None, news: dict | None,
                 analysis: str | None, cache: dict) -> str:
    lines = [
        "# ⏱️ Gold — 15-Minute Sentiment Report",
        "",
        f"**Last updated:** {utc_now()}",
        "",
    ]

    if price:
        lines += [
            "## 📈 Intraday Price",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Last price | **{price.get('last_price', 'n/a')}** |",
            f"| Session change | {price.get('session_change', 'n/a')} "
            f"({price.get('session_change_pct', 'n/a')}%) |",
            f"| Session high / low | {price.get('session_high', 'n/a')} / "
            f"{price.get('session_low', 'n/a')} |",
            f"| Source | {price.get('source', 'n/a')} |",
            "",
        ]
    elif cache.get("price"):
        cached = cache["price"]
        lines += [
            "## 📈 Intraday Price (⚠️ cached — live sources unavailable)",
            "",
            f"- Last known price: **{cached.get('last_price', 'n/a')}** "
            f"(from {cache.get('updated_at', 'unknown time')})",
            f"- Source: {cached.get('source', 'n/a')}",
            "",
        ]
    else:
        lines += [
            "## 📈 Intraday Price",
            "",
            "> ⚠️ **Fallback state:** all live price sources and the local cache were "
            "unavailable this run. This is a graceful degradation, not an error — "
            "the next run will retry automatically.",
            "",
        ]

    lines += ["## 🧭 Retail / Broker Positioning", ""]
    if sentiment:
        lines += [
            f"- Retail long: **{sentiment.get('retail_long_pct', 'n/a')}%** · "
            f"Retail short: **{sentiment.get('retail_short_pct', 'n/a')}%**",
            f"- Source: {sentiment.get('source', 'n/a')}",
            "",
        ]
    elif cache.get("sentiment"):
        cached = cache["sentiment"]
        lines += [
            f"- ⚠️ Cached ({cache.get('updated_at', 'unknown time')}): "
            f"long {cached.get('retail_long_pct', 'n/a')}% / "
            f"short {cached.get('retail_short_pct', 'n/a')}%",
            "",
        ]
    else:
        lines += ["- ⚠️ No positioning data available this run (all sources failed).", ""]

    if news and news.get("headlines"):
        lines += ["## 📰 Latest Headlines", ""]
        for feed, titles in news["headlines"].items():
            lines.append(f"**{feed}**")
            for t in titles[:5]:
                lines.append(f"- {t}")
            lines.append("")

    lines += ["## 🤖 AI Analysis (Claude Haiku)", ""]
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
        "*Auto-generated every 15 minutes by "
        "[`scripts/15min_sentiment.py`](scripts/15min_sentiment.py). "
        "Not investment advice.*",
        "",
    ]
    return "\n".join(lines)


def update_readme_section(report_md: str) -> None:
    """Splice the report into README.md between the 15MIN markers, if present."""
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
    print(f"[info] 15-minute sentiment run starting at {utc_now()}")
    cache = load_cache()

    price = fetch_price()
    sentiment = fetch_myfxbook_sentiment()
    news = fetch_news_headlines()

    analysis = None
    if price or sentiment or news:
        analysis = analyze_with_claude({
            "timestamp_utc": utc_now(),
            "price": price,
            "retail_sentiment": sentiment,
            "news": news,
            "note": "Some fields may be null if a source was unavailable.",
        })

    report = build_report(price, sentiment, news, analysis, cache)
    REPORT_PATH.write_text(report)
    print(f"[info] wrote {REPORT_PATH}")

    update_readme_section(report)

    # Persist whatever we successfully fetched for the next run's fallback.
    new_cache = {
        "updated_at": utc_now(),
        "price": price or cache.get("price"),
        "sentiment": sentiment or cache.get("sentiment"),
    }
    save_cache(new_cache)
    print("[info] run complete")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Absolute last resort: write a fallback notice so the file always exists.
        traceback.print_exc()
        try:
            REPORT_PATH.write_text(
                "# ⏱️ Gold — 15-Minute Sentiment Report\n\n"
                f"**Last attempted update:** {utc_now()}\n\n"
                "> ⚠️ **Fallback state:** an unexpected error occurred this run. "
                "The previous report content was replaced with this notice; the "
                "next scheduled run will retry automatically.\n"
            )
        except Exception:
            pass
    sys.exit(0)
