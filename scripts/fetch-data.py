#!/usr/bin/env python3
"""Fetch checkpoints, weather, PSI and holidays for the SG-MY dashboard.

Writes data.json at the repo root. Runs hourly from
.github/workflows/update-data.yml. Stdlib only — no pip installs.
Secrets come from the environment (GitHub Secrets in the workflow):
LTA_API_KEY, OPENWEATHER_API_KEY. Every section is independent: a
failed or unconfigured source keeps its last-known values from the
existing data.json, and the page falls back to its baked snapshot
when a section is absent. Exit 0 unless the whole run crashes.

Verified live 2026-11-16:
- LTA DataMall Traffic Speed Bands v4 returns LinkID, RoadName,
  RoadCategory, SpeedBand (1-8), MinimumSpeed/MaximumSpeed (km/h),
  StartLat/StartLon/EndLat/EndLon. RoadName is the full name
  ("BUKIT TIMAH EXPRESSWAY", "AYER RAJAH EXPRESSWAY"). Direction is
  worked out from the link geometry: the end nearer the checkpoint
  is the end traffic is heading towards. Approach-road speed only —
  never presented as a clearance time. VERIFY ON FIRST RUN: compare
  the four speeds with the preview snapshot (Woodlands to JB 14,
  from JB 66, Tuas to JB 48, from JB 31 km/h); if a direction is
  flipped, pin the LinkID in CHECKPOINT_LINKS below.
- Nager.Date covers Singapore (HTTP 200) but NOT Malaysia: /MY
  returns HTTP 204 and MY is absent from /AvailableCountries. So MY
  federal holidays come only from the curated
  data/my-holidays-{year}.json (Prime Minister's Department federal
  schedule), and Johor state holidays only from
  data/johor-holidays.json (no API covers MY state holidays).
  Update both files once a year from the gazette.
- PSI: data.gov.sg v1/environment/psi, no key.
- Weather: OpenWeather current weather + 3-hour forecast (pop),
  key as a Secret, same key Charlie uses.

Output shape — the page reads exactly this (verified against the
page source 2026-11-16; anything missing falls back to the baked
snapshot per field, never an error):
  { generatedAt,
    checkpoints: {directions: {woodlands_sg_jb, woodlands_jb_sg,
      tuas_sg_jb, tuas_jb_sg: {speedKmh, band, status, road,
      incident?}}, updatedAt},
    weather: {sg, jb, muar: {temp, cond, feels, advice, rain, hum,
      wind, rainLabel, bar}, updatedAt},
    psi: {regions: {central, west, south, east, north}, overall,
      band, advice, updatedAt},
    holidays: {all: [{name, iso, scopes, replacement, johorOnly,
      sub}], nextLongWeekend?, updatedAt} }
"""
import json
import math
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
DATA_DIR = os.path.join(ROOT, "data")

LTA_SPEED_BANDS = "https://datamall2.mytransport.sg/ltaodataservice/v4/TrafficSpeedBands"
LTA_INCIDENTS = "https://datamall2.mytransport.sg/ltaodataservice/TrafficIncidents"
OPENWEATHER_CURRENT = ("https://api.openweathermap.org/data/2.5/weather"
                       "?lat={lat}&lon={lon}&appid={key}&units=metric")
OPENWEATHER_FORECAST = ("https://api.openweathermap.org/data/2.5/forecast"
                        "?lat={lat}&lon={lon}&appid={key}&units=metric&cnt=1")
PSI_URL = "https://api.data.gov.sg/v1/environment/psi"
NAGER_SG = "https://date.nager.at/api/v3/PublicHolidays/{year}/SG"

CITIES = {
    "sg": ("Singapore", 1.3521, 103.8198),
    "jb": ("Johor Bahru", 1.4927, 103.7414),
    "muar": ("Muar", 2.0442, 102.5689),
}
# Checkpoint approach points. The link end nearer the checkpoint is
# the end traffic is heading towards ("to"); the other end is "from".
CHECKPOINTS = {
    "woodlands_sg_jb": (("BUKIT TIMAH", "BKE"), (1.447, 103.786), "to"),
    "woodlands_jb_sg": (("BUKIT TIMAH", "BKE"), (1.447, 103.786), "from"),
    "tuas_sg_jb":      (("AYER RAJAH", "AYE"), (1.321, 103.635), "to"),
    "tuas_jb_sg":      (("AYER RAJAH", "AYE"), (1.321, 103.635), "from"),
}
# Optional pinning: {"woodlands_sg_jb": "LinkID", ...} — fill in after
# the first verified run if the nearest-link heuristic picks wrong.
CHECKPOINT_LINKS = {}


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def load_existing():
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                return json.load(f)
        except Exception as e:
            print(f"data.json unreadable ({e}); starting fresh", file=sys.stderr)
    return {}


def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def status_for(speed):
    if speed < 20:
        return "Jammed"
    if speed < 40:
        return "Busy"
    if speed < 60:
        return "Moderate"
    return "Smooth"


def fetch_speed_band_links(key):
    """Fetch all TrafficSpeedBands pages. v4 pages at 500 records and
    the BKE/AYE approach links sit beyond the first page, so follow
    @odata.nextLink (falling back to $skip) until a page adds nothing
    new. Links are deduplicated by LinkID."""
    links = []
    seen_ids = set()
    seen_urls = set()
    url = LTA_SPEED_BANDS
    for _ in range(12):  # up to ~6000 links
        if url in seen_urls:
            break
        seen_urls.add(url)
        data = get_json(url, headers={"AccountKey": key})
        page = data.get("value", [])
        new = 0
        for link in page:
            lid = link.get("LinkID")
            if lid is None or lid in seen_ids:
                continue
            seen_ids.add(lid)
            links.append(link)
            new += 1
        if new == 0:
            break
        nxt = data.get("@odata.nextLink") or data.get("nextLink")
        if nxt:
            url = nxt if nxt.startswith("http") else "https://datamall2.mytransport.sg" + nxt
            continue
        if len(page) == 500:
            sep = "&" if "?" in LTA_SPEED_BANDS else "?"
            url = f"{LTA_SPEED_BANDS}{sep}$skip={len(links)}"
            continue
        break
    return links


def fetch_checkpoints(existing):
    key = os.environ.get("LTA_API_KEY")
    if not key:
        print("Checkpoints: no LTA_API_KEY, keeping last-known", file=sys.stderr)
        return existing.get("checkpoints")
    try:
        links = fetch_speed_band_links(key)
    except Exception as e:
        print(f"Checkpoints fetch failed ({e}); keeping last-known", file=sys.stderr)
        return existing.get("checkpoints")
    print(f"Checkpoints: fetched {len(links)} speed-band links")
    if links:
        def _count(*names):
            return sum(1 for l in links
                       if any(n in (l.get("RoadName") or "").upper() for n in names))
        print(f"Checkpoints: BKE links: {_count('BUKIT TIMAH', 'BKE')}, "
              f"AYE links: {_count('AYER RAJAH', 'AYE')}")
    incident = None
    try:
        inc = get_json(LTA_INCIDENTS, headers={"AccountKey": key})
        for item in inc.get("value", []):
            msg = item.get("Message") or ""
            low = msg.lower()
            if "heavy traffic" in low and any(
                    k in low for k in ("woodlands", "tuas", "checkpoint",
                                      "causeway", "second link")):
                incident = msg
                break
    except Exception as e:
        print(f"Checkpoints incidents failed ({e})", file=sys.stderr)
    out = {}
    for name, (road_names, point, direction) in CHECKPOINTS.items():
        pinned = CHECKPOINT_LINKS.get(name)
        cands = []
        for link in links:
            road = (link.get("RoadName") or "").upper()
            if not any(r in road for r in road_names):
                continue
            if pinned and str(link.get("LinkID")) != str(pinned):
                continue
            try:
                sp = (float(link["StartLat"]), float(link["StartLon"]))
                ep = (float(link["EndLat"]), float(link["EndLon"]))
            except (KeyError, TypeError, ValueError):
                continue
            mid = ((sp[0] + ep[0]) / 2, (sp[1] + ep[1]) / 2)
            link_dir = "to" if dist(ep, point) < dist(sp, point) else "from"
            if link_dir != direction:
                continue
            cands.append((dist(mid, point), link))
        if not cands:
            road_hits = sum(1 for link in links
                            if any(r in (link.get("RoadName") or "").upper()
                                   for r in road_names))
            print(f"Checkpoints: no link for {name} (road matches: {road_hits}); "
                  "keeping last-known", file=sys.stderr)
            prev = (existing.get("checkpoints") or {}).get("directions", {}).get(name)
            if prev:
                out[name] = prev
            continue
        cands.sort(key=lambda c: c[0])
        link = cands[0][1]
        try:
            band = int(link["SpeedBand"])
            speed = (float(link["MinimumSpeed"]) + float(link["MaximumSpeed"])) / 2
        except (KeyError, TypeError, ValueError):
            continue
        entry = {"speedKmh": speed, "band": band, "status": status_for(speed),
                 "road": link.get("RoadName")}
        if incident:
            entry["incident"] = incident
        out[name] = entry
        print(f"Checkpoints {name}: {speed} km/h ({entry['status']})")
    if not out:
        return existing.get("checkpoints")
    return {"directions": out, "updatedAt": now_iso()}


def advice_for(wid, cond):
    if 200 <= wid < 300:
        return "Thunderstorms — stay indoors if you can."
    if 300 <= wid < 600:
        return "Rain expected — bring an umbrella."
    if wid == 800:
        return "Clear skies — hot and sunny, stay hydrated."
    if 801 <= wid <= 804:
        return "Cloudy — warm and humid."
    return f"{cond} — warm and humid."


def fetch_weather(existing):
    key = os.environ.get("OPENWEATHER_API_KEY")
    if not key:
        print("Weather: no OPENWEATHER_API_KEY, keeping last-known", file=sys.stderr)
        return existing.get("weather")
    out = {}
    for city_key, (name, lat, lon) in CITIES.items():
        try:
            cur = get_json(OPENWEATHER_CURRENT.format(lat=lat, lon=lon, key=key))
            fc = get_json(OPENWEATHER_FORECAST.format(lat=lat, lon=lon, key=key))
            main = cur.get("main", {})
            w = (cur.get("weather") or [{}])[0]
            wid = int(w.get("id", 0))
            cond = (w.get("description") or "").capitalize()
            pop = 0.0
            try:
                pop = float((fc.get("list") or [{}])[0].get("pop", 0.0))
            except (TypeError, ValueError):
                pass
            rain_pct = round(pop * 100)
            if pop >= 0.5:
                label = f"{rain_pct}% — likely"
            elif pop >= 0.3:
                label = f"{rain_pct}% — possible"
            else:
                label = f"{rain_pct}% — unlikely"
            out[city_key] = {
                "temp": f"{main.get('temp', 0):.0f}°",
                "cond": cond,
                "feels": (f"Feels like {main.get('feels_like', 0):.0f}° · "
                          f"H {main.get('temp_max', 0):.0f}° / "
                          f"L {main.get('temp_min', 0):.0f}°"),
                "advice": advice_for(wid, cond),
                "rain": f"{rain_pct}%",
                "hum": f"{main.get('humidity', 0)}%",
                "wind": f"{cur.get('wind', {}).get('speed', 0) * 3.6:.0f} km/h",
                "rainLabel": label,
                "bar": f"{rain_pct}%",
            }
            print(f"Weather {name}: {out[city_key]['temp']}, {cond}")
        except Exception as e:
            print(f"Weather {name} failed ({e}); keeping last-known", file=sys.stderr)
            prev = (existing.get("weather") or {}).get(city_key)
            if prev:
                out[city_key] = prev
    if not out:
        return existing.get("weather")
    out["updatedAt"] = now_iso()
    return out


def fetch_psi(existing):
    try:
        data = get_json(PSI_URL)
        item = (data.get("items") or [{}])[0]
        readings = item.get("readings", {}).get("psi_twenty_four_hourly", {})
    except Exception as e:
        print(f"PSI fetch failed ({e}); keeping last-known", file=sys.stderr)
        return existing.get("psi")
    regions = {k: v for k, v in readings.items()
               if k in ("central", "west", "south", "east", "north")
               and isinstance(v, (int, float))}
    if not regions:
        return existing.get("psi")
    overall = max(regions.values())
    if overall <= 50:
        band, advice = "GOOD", "Air quality is good — fine for outdoor activities."
    elif overall <= 100:
        band, advice = "MODERATE", "Air quality is acceptable."
    elif overall <= 200:
        band, advice = "UNHEALTHY", "Unhealthy air — reduce prolonged outdoor exertion."
    elif overall <= 300:
        band, advice = "VERY UNHEALTHY", "Avoid outdoor activities."
    else:
        band, advice = "HAZARDOUS", "Stay indoors."
    return {"regions": regions, "overall": overall, "band": band,
            "advice": advice, "updatedAt": now_iso()}


# Name aliases so the same holiday from different feeds merges into
# one row (e.g. Nager SG "Hari Raya Puasa" == MY "Hari Raya Aidilfitri").
ALIASES = {
    "hari raya puasa": "hari raya aidilfitri",
    "hari raya haji": "hari raya aidiladha",
    "vesak day": "wesak day",
    "prophet muhammad's birthday": "birthday of the prophet muhammad",
}


def norm_name(name):
    n = name.lower().strip()
    n = re.sub(r"\s*\((second day|day 2|day 1|replacement)\)\s*", "", n)
    n = re.sub(r"^substitute holiday for ", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return ALIASES.get(n, n)


def fetch_holidays(existing):
    years = [datetime.now(timezone.utc).year, datetime.now(timezone.utc).year + 1]
    merged = {}
    # Singapore — Nager.Date (verified live: HTTP 200).
    for y in years:
        try:
            for h in get_json(NAGER_SG.format(year=y)):
                if h.get("types") and "Public" not in h["types"]:
                    continue
                name = h.get("localName") or h.get("name") or ""
                iso = h.get("date") or ""
                if not name or not iso:
                    continue
                replacement = bool(re.search(r"observed|in lieu", name, re.I))
                clean = re.sub(r"\s*\(observed\)\s*", "", name, flags=re.I).strip()
                key = (iso, norm_name(clean))
                item = merged.setdefault(key, {"name": clean, "iso": iso,
                                               "scopes": [],
                                               "replacement": replacement})
                if "sg" not in item["scopes"]:
                    item["scopes"].append("sg")
                item["replacement"] = item["replacement"] or replacement
            print(f"Holidays SG {y}: ok")
        except Exception as e:
            print(f"Holidays SG {y} failed ({e})", file=sys.stderr)
    # Malaysia federal + Johor state — curated files only (Nager.Date
    # does NOT cover Malaysia: /MY returns HTTP 204).
    for fname, scope in [(f"my-holidays-{y}.json", "my") for y in years]:
        path = os.path.join(DATA_DIR, fname)
        entries = _load_curated(path)
        for e in entries:
            _add_holiday(merged, e, scope)
    for e in _load_curated(os.path.join(DATA_DIR, "johor-holidays.json")):
        _add_holiday(merged, e, "johor-only", johor_only=True)
    # SG observed days: Nager.Date lists only the observed Monday, with
    # no marker that it is a replacement (MOM gazette). Tag them from
    # the curated list; the dates here are the replacement dates.
    for e in _load_curated(os.path.join(DATA_DIR, "sg-replacements.json")):
        key = (e.get("date", ""), norm_name(e.get("name", "")))
        if key in merged:
            merged[key]["replacement"] = True
    all_items = sorted(merged.values(), key=lambda x: x["iso"])
    if not all_items:
        print("Holidays: nothing fetched; keeping last-known", file=sys.stderr)
        return existing.get("holidays")
    out = {"all": all_items, "updatedAt": now_iso()}
    lw = next_long_weekend(all_items)
    if lw:
        out["nextLongWeekend"] = lw
    return out


def _load_curated(path):
    if not os.path.exists(path):
        print(f"Holidays: {os.path.basename(path)} not found, skipped",
              file=sys.stderr)
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"Holidays: {path} unreadable ({e})", file=sys.stderr)
        return []


def _add_holiday(merged, e, scope, johor_only=False):
    iso, name = e.get("date", ""), e.get("name", "")
    if not iso or not name:
        return
    key = (iso, norm_name(name))
    item = merged.setdefault(key, {"name": name, "iso": iso, "scopes": [],
                                   "replacement": bool(e.get("replacement"))})
    if scope not in item["scopes"]:
        item["scopes"].append(scope)
    if johor_only:
        item["johorOnly"] = True
    item["replacement"] = item["replacement"] or bool(e.get("replacement"))
    if e.get("note"):
        item.setdefault("sub", e["note"])


def next_long_weekend(items):
    """First upcoming Fri/Mon holiday (or adjacent pair bridging a weekend)."""
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=90)
    upcoming = []
    for it in items:
        try:
            d = date.fromisoformat(it["iso"])
        except ValueError:
            continue
        if today <= d <= horizon:
            upcoming.append((d, it))
    upcoming.sort(key=lambda t: t[0])
    for i, (d, it) in enumerate(upcoming):
        wd = d.weekday()  # Mon=0 ... Sun=6
        if wd == 4:  # Friday
            end = d + timedelta(days=2)
            return {"title": f"{it['name']} long weekend",
                    "detail": f"{d.strftime('%a %d %b')} – {end.strftime('%a %d %b')} · 3 days"}
        if wd == 0:  # Monday
            start = d - timedelta(days=2)
            return {"title": f"{it['name']} long weekend",
                    "detail": f"{start.strftime('%a %d %b')} – {d.strftime('%a %d %b')} · 3 days"}
        if i + 1 < len(upcoming):
            d2, it2 = upcoming[i + 1]
            if (d2 - d).days == 1 and (d.weekday() == 3 or d2.weekday() == 0):
                start = d - timedelta(days=d.weekday())
                end = d2 + timedelta(days=(6 - d2.weekday()))
                days = (end - start).days + 1
                return {"title": f"{it['name']} + {it2['name']} long weekend",
                        "detail": f"{start.strftime('%a %d %b')} – {end.strftime('%a %d %b')} · {days} days"}
    return None


def main():
    existing = load_existing()
    out = {
        "generatedAt": now_iso(),
        "checkpoints": fetch_checkpoints(existing),
        "weather": fetch_weather(existing),
        "psi": fetch_psi(existing),
        "holidays": fetch_holidays(existing),
    }
    out = {k: v for k, v in out.items() if v is not None}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
