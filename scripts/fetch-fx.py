#!/usr/bin/env python3
"""Fetch Wise + CIMB SGD->MYR rates. Writes rates.json at the repo root.

  - Wise: public rates/live endpoint, full precision, no key.
  - CIMB: scraped from the cimbclicks.com.sg/sgd-to-myr page (hidden
    rateList field), re-scraped at most every 15 minutes — the last-known
    value is kept in between, because CIMB's board rate only changes a few
    times a day and scraping is the bigger bot-block risk.

A failed source keeps its last-known value; nothing is guessed. If neither
rate is available and there is no previous file, nothing is written.
The page reads the live rate from the Cloudflare Worker first — rates.json
is the fallback when the Worker omits a rate or is down.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
RATES_FILE = ROOT / "rates.json"
SGT = timezone(timedelta(hours=8))
NOW = datetime.now(SGT)
UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36")}


def load_previous():
    try:
        return json.loads(RATES_FILE.read_text())
    except Exception:
        return {}


def age_minutes(iso):
    try:
        return (NOW - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return 1e9


def fetch_wise():
    try:
        r = requests.get("https://wise.com/rates/live?source=SGD&target=MYR",
                         timeout=15)
        r.raise_for_status()
        v = r.json().get("value")
        return float(v) if isinstance(v, (int, float)) else None
    except Exception as e:
        print(f"wise failed: {e}", file=sys.stderr)
        return None


def fetch_cimb():
    try:
        r = requests.get("https://www.cimbclicks.com.sg/sgd-to-myr",
                         headers=UA, timeout=20)
        r.raise_for_status()
        m = (re.search(r'rateList[\s\S]{0,300}?\[(\d+\.\d+)\]', r.text)
             or re.search(r'"rateList"\s*:\s*"\[(\d+\.\d+)\]"', r.text))
        if m:
            return float(m.group(1))
        print("cimb: rateList not found in page", file=sys.stderr)
    except Exception as e:
        print(f"cimb failed: {e}", file=sys.stderr)
    return None


def prev_rate_info(prev, key, flat_key):
    """(rate, updatedAt) from a previous rates.json. Accepts both the
    contract shape {key: {rate|midRate, updatedAt}} and the older flat
    shape {key: number, flat_key: timestamp}."""
    v = prev.get(key)
    if isinstance(v, dict):
        rate = v.get("rate") if key == "cimb" else (v.get("midRate") or v.get("rate"))
        ts = v.get("updatedAt")
    elif isinstance(v, (int, float)):
        rate, ts = float(v), prev.get(flat_key)
    else:
        rate, ts = None, None
    return rate, ts


def main():
    prev = load_previous()
    prev_cimb_rate, prev_cimb_updated = prev_rate_info(prev, "cimb", "cimb_updated_at")
    wise = fetch_wise()
    if wise is not None:
        wise_updated = NOW.isoformat()
    else:
        wise, wise_updated = prev_rate_info(prev, "wise", "wise_updated_at")
        if wise is not None:
            print("wise: kept last-known", file=sys.stderr)
    cimb = None
    cimb_updated = None
    if prev_cimb_rate and prev_cimb_updated and age_minutes(prev_cimb_updated) < 15:
        cimb, cimb_updated = float(prev_cimb_rate), prev_cimb_updated
        print("cimb: kept last-known (fresh)", file=sys.stderr)
    else:
        cimb = fetch_cimb()
        if cimb is not None:
            cimb_updated = NOW.isoformat()
        elif prev_cimb_rate:
            cimb, cimb_updated = float(prev_cimb_rate), prev_cimb_updated
            print("cimb: kept last-known (scrape failed)", file=sys.stderr)
    out = {}
    if cimb is not None:
        out["cimb"] = {"rate": cimb, "updatedAt": cimb_updated or NOW.isoformat()}
    if wise is not None:
        out["wise"] = {"midRate": wise, "updatedAt": wise_updated or NOW.isoformat()}
    if not out:
        print("no rates at all — not writing rates.json", file=sys.stderr)
        return
    out["updatedAt"] = NOW.isoformat()
    RATES_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
