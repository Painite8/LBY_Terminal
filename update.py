#!/usr/bin/env python3
"""
LBY Terminal — daily data updater.

Runs once a day inside GitHub Actions. Asks Claude (with web search) to
research the current state of the Indonesian market and return a single JSON
object matching the dashboard's data structure. Writes that to data.json.

Safety design: if anything goes wrong (API error, bad JSON, missing key),
the script exits WITHOUT overwriting data.json, so the dashboard always keeps
the last good data instead of breaking.
"""

import os
import re
import sys
import json
import datetime
import anthropic

MODEL = "claude-sonnet-4-6"  # fastest + cheapest; supports web search
DATA_FILE = "data.json"

# ---- The JSON shape the dashboard expects. We give this to Claude verbatim. ----
SCHEMA_EXAMPLE = {
    "meta": {"updated": "Fri, 6 Jun 2025 · 14:23 WIB",
             "marketStatus": {"label": "IDX Closed", "tone": "neg"}},
    "tickers": [
        {"lbl": "JCI (IHSG)", "val": "7,234", "chg": -0.82},
        {"lbl": "IDR/USD", "val": "15,890", "chg": -0.31},
        {"lbl": "BI Rate", "val": "6.25%", "chg": 0, "note": "Hold"},
        {"lbl": "Govt Bond 10Y", "val": "6.78%", "chg": 0.04},
        {"lbl": "Coal", "val": "$127", "chg": 1.2},
        {"lbl": "Nickel", "val": "$16,450", "chg": 2.1},
        {"lbl": "CPO", "val": "3,890", "chg": -0.5},
    ],
    "heroes": [
        {"type": "uncertainty", "score": 67, "level": "High",
         "note": "Drivers of the score in one sentence."},
        {"type": "outlook", "stance": "Cautiously Bearish", "tone": "neg",
         "duration": "2-3 weeks", "note": "One-sentence rationale."},
        {"type": "flow", "net3d": "-Rp 2.3T", "ytd": "-Rp 8.1T",
         "topSells": "BBCA · TLKM · BMRI"},
    ],
    "commodities": [
        {"name": "Coal (Newcastle)", "sub": "→ ADRO, PTBA, ITMG",
         "price": "$127", "unit": "/ton", "chg": 1.2},
    ],
    "sectors": [{"name": "Mining", "chg": 2.1}],
    "news": [
        {"title": "Headline in English", "sentiment": "pos",
         "source": "Kontan", "cred": 4, "time": "2h ago",
         "sector": "Mining", "impact": "What it means for the market."},
    ],
    "regulations": [
        {"title": "Mining Bill", "sub": "Passed parliament · 2 days ago",
         "status": "Enacted", "tone": "pos", "sector": "Mining"},
    ],
    "calendar": [
        {"event": "Bank Indonesia Rate Meeting", "detail": "Rate decision",
         "date": "19 Jun", "days": 13, "tone": "warn"},
    ],
    "ratings": [
        {"agency": "S&P", "grade": "BBB", "outlook": "Stable",
         "action": "Affirmed Apr 2025", "tone": "neu"},
    ],
    "precedents": [{"html": "Plain sentence; wrap key numbers in <strong>...</strong>."}],
    "geo": [{"html": "Chain sentence ending with a colored Now: clause."}],
}

PROMPT = f"""You are the data engine for "LBY Terminal", a daily Indonesian
financial-market intelligence dashboard. Today is {{today}}.

Use web search to research the CURRENT state of the Indonesian market and
return ONE JSON object that exactly matches this structure (same keys, same
value types). All text must be in English.

STRUCTURE (example values — replace with real, current data):
{json.dumps(SCHEMA_EXAMPLE, indent=2, ensure_ascii=False)}

Rules:
- Research and fill in: JCI (IHSG) level + % change, IDR/USD, BI rate, 10Y govt
  bond yield, and commodity prices for coal, nickel, palm oil (CPO), rubber, gold.
- "tickers": 7 entries in the order shown.
- "commodities": 5 entries (coal, nickel, CPO, rubber, gold), each with a "sub"
  field naming the Indonesian stocks it most affects.
- "sectors": 7 IDX sectors with realistic daily % change (Mining, Infrastructure,
  Energy, Telco, Banking, Plantation, Consumer).
- "news": 5-7 of the most market-relevant Indonesian headlines from the last ~48h.
  "sentiment" is one of "pos"/"neg"/"neu". "cred" is source credibility 1-4
  (4 = Reuters/Bloomberg/Kontan/Bisnis/Antara, 1 = anonymous blog/forum).
  "impact" = one sentence on what it means for the market and rough time horizon.
- "heroes": exactly the 3 objects shown (uncertainty, outlook, flow).
  - uncertainty.score 0-100; level is "Low"/"Moderate"/"High".
    Base it on: foreign outflows, IDR move, market volatility, unresolved major
    legislation, macro revisions, bond-yield moves. Briefly state the drivers.
  - outlook.tone is "pos"/"neg"/"neu"; duration like "2-3 weeks".
  - flow: foreign net buy/sell over ~3 days and YTD (use "Rp ...T"), plus the
    most-sold tickers.
- "regulations": 3-5 recent/pending UU, Perpres, or PP. "status" short label,
  "tone" one of "pos"/"neg"/"neu"/"warn".
- "calendar": 3-5 upcoming economic events (BI meeting, Fed, BPS releases) with
  date, days-until ("days" integer), and "tone". Add "done": true if past.
- "ratings": S&P, Moody's, Fitch — current sovereign grade + outlook.
- "precedents": 3 short historical-analogy sentences. Wrap key figures in <strong>.
- "geo": 3 geopolitical transmission-chain sentences (e.g. US-China → commodity
  demand → exports → IDX Mining), each ending with a "Now: ..." status.
- "meta.updated": current Jakarta time, format like "Fri, 6 Jun 2025 · 14:23 WIB".
- "meta.marketStatus.tone": "pos" if IDX up today, "neg" if down, "neu" if flat.

Output ONLY the JSON object as your final message — no markdown fences, no
commentary before or after it.
""".replace("{today}", datetime.date.today().strftime("%A, %d %B %Y"))


# ---------- presentational fields the dashboard needs, filled in Python ----------
TONE_TO_DOT = {"pos": "var(--green)", "neg": "var(--red)",
               "warn": "var(--amber)", "neu": "var(--text-faint)"}
LEVEL_TO_COLOR = {"Low": "var(--green)", "Moderate": "var(--amber)", "High": "var(--amber)"}


def extract_json(text):
    """Pull the JSON object out of Claude's final text, tolerating stray prose/fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in model output")
    return json.loads(text[start:end + 1])


def add_presentation(data):
    """Add the CSS-variable fields (color/dot) the template renders, from tone/level."""
    for h in data.get("heroes", []):
        if h.get("type") == "uncertainty":
            h["color"] = LEVEL_TO_COLOR.get(h.get("level"), "var(--amber)")
    for r in data.get("regulations", []):
        r["dot"] = TONE_TO_DOT.get(r.get("tone"), "var(--text-faint)")
    return data


REQUIRED_KEYS = ["meta", "tickers", "heroes", "commodities", "sectors",
                 "news", "regulations", "calendar", "ratings", "precedents", "geo"]


def main():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=key)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": PROMPT}],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 8,
                "user_location": {"type": "approximate", "city": "Jakarta",
                                  "region": "Jakarta", "country": "ID",
                                  "timezone": "Asia/Jakarta"},
            }],
        )
    except Exception as e:
        print(f"ERROR calling Claude: {e}", file=sys.stderr)
        sys.exit(1)

    # The final answer is in the text block(s); concatenate them.
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    try:
        data = extract_json(text)
    except Exception as e:
        print(f"ERROR parsing JSON: {e}", file=sys.stderr)
        print("--- raw output ---\n" + text[:2000], file=sys.stderr)
        sys.exit(1)

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        print(f"ERROR: response missing keys: {missing}", file=sys.stderr)
        sys.exit(1)

    data = add_presentation(data)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"OK: wrote {DATA_FILE} with {len(data['news'])} news items")


if __name__ == "__main__":
    main()
