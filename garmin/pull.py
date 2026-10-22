#!/usr/bin/env python3
"""Pull Garmin data through the official Personal API into data/ as JSON.

Usage:
  python3 pull.py            last 12 months on the first run, then only what's new
  python3 pull.py --full     full history (back to the oldest activity)
  python3 pull.py --days 90  first-run window in days (default 365)

Reads GARMIN_PERSONAL_API_KEY from .env.local (or the environment).
Standard library only, nothing to install.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE = "https://personal.connectapi.garmin.com"
TOKEN_URL = BASE + "/personalapitoken-service/personal-api/personal-api-key/token"
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
TOKEN_MAX_AGE_H = 20
PAGE = 20
PAUSE_S = 0.3
OVERLAP_DAYS = 3  # re-fetch the last few days so late watch syncs land


class TooBig(Exception):
    """HTTP 500 with an empty body: the response was over ~256 KB."""


def log(msg):
    print(msg, flush=True)


def load_key():
    key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    env = HERE / ".env.local"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GARMIN_PERSONAL_API_KEY="):
                key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        sys.exit("GARMIN_PERSONAL_API_KEY is not set. Paste your key into garmin/.env.local.")
    return key


def read_json(name, default):
    path = DATA / name
    if path.exists():
        return json.loads(path.read_text())
    return default


def write_json(name, obj):
    path = DATA / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
    tmp.replace(path)


def get_token(key):
    cached = read_json("token.json", {})
    if cached.get("token") and time.time() - cached.get("obtained_at", 0) < TOKEN_MAX_AGE_H * 3600:
        return cached["token"]
    req = urllib.request.Request(TOKEN_URL, method="POST", headers={"Authorization": "ApiKey " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = r.read().decode().strip()
    except urllib.error.HTTPError as e:
        sys.exit(f"Token request failed (HTTP {e.code}). The key in .env.local is missing, mistyped or expired.")
    write_json("token.json", {"token": token, "obtained_at": time.time()})
    os.chmod(DATA / "token.json", 0o600)
    return token


def api_get(token, path):
    req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    for attempt in range(5):
        time.sleep(PAUSE_S)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as e:
            if e.code == 500:
                raise TooBig(path)
            if e.code in (429, 502, 503) and attempt < 4:
                time.sleep(3 * (attempt + 1))
                continue
            if e.code == 404:
                return None
            raise
        except urllib.error.URLError:
            if attempt < 4:
                time.sleep(3 * (attempt + 1))
                continue
            raise


def ranges(start, end, days):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def fetch_range(token, make_path, start, end):
    """GET one date range, halving it on 500 (response too big)."""
    try:
        return [api_get(token, make_path(start, end))]
    except TooBig:
        if start == end:
            log(f"  skipping {start}: response too big even for one day")
            return []
        mid = start + (end - start) // 2
        return fetch_range(token, make_path, start, mid) + fetch_range(token, make_path, mid + timedelta(days=1), end)


def pull_activities(token, full, since):
    store = {str(a["activityId"]): a for a in read_json("activities.json", [])}
    known = set(store)
    start, new = 0, 0
    while True:
        page = api_get(token, f"/activitylist-service/activities/search/activities?start={start}&limit={PAGE}") or []
        for a in page:
            if str(a["activityId"]) not in store:
                new += 1
            store[str(a["activityId"])] = a
        log(f"  activities: {len(store)} stored ({new} new)")
        oldest = page[-1].get("startTimeLocal", "")[:10] if page else ""
        if len(page) < PAGE:
            break
        if not full and known and any(str(a["activityId"]) in known for a in page):
            break
        if not full and oldest and oldest < since.isoformat():
            break
        start += PAGE
    acts = sorted(store.values(), key=lambda a: a.get("startTimeLocal", ""), reverse=True)
    write_json("activities.json", acts)
    return acts


def pull_dated(token, name, label, make_path, extract, date_key, start, end, chunk):
    store = read_json(name, {})
    total = (end - start).days + 1
    done = 0
    for a, b in ranges(start, end, chunk):
        for resp in fetch_range(token, make_path, a, b):
            for item in extract(resp) or []:
                d = item.get(date_key)
                if d:
                    store[d[:10]] = item
        done += (b - a).days + 1
        log(f"  {label}: {done}/{total} days")
        write_json(name, store)
    return store


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="pull full history")
    ap.add_argument("--days", type=int, default=365, help="first-run window in days (default 365)")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    token = get_token(load_key())
    state = read_json("state.json", {})
    today = date.today()
    first_run_since = today - timedelta(days=args.days)

    log("Activities")
    acts = pull_activities(token, args.full, first_run_since)

    if args.full and acts:
        default_start = date.fromisoformat(acts[-1]["startTimeLocal"][:10])
    else:
        default_start = first_run_since

    def since(key):
        if args.full:
            return default_start
        last = state.get(key)
        if last:
            return date.fromisoformat(last) - timedelta(days=OVERLAP_DAYS)
        return default_start

    log("Daily summaries")
    daily = pull_dated(
        token, "daily.json", "daily",
        lambda a, b: f"/usersummary-service/v2/dailysummaries?startDate={a}&endDate={b}",
        lambda r: (r or {}).get("data"), "calendarDate", since("daily"), today, 60)
    state["daily"] = today.isoformat()

    log("Sleep")
    sleep = pull_dated(
        token, "sleep.json", "sleep",
        lambda a, b: f"/sleep-service/sleep/dailySleepSummariesByDate?startDate={a}&endDate={b}",
        lambda r: r if isinstance(r, list) else [], "calendarDate", since("sleep"), today, 31)
    sleep = {d: s for d, s in sleep.items() if s.get("sleepTimeSeconds")}
    write_json("sleep.json", sleep)
    state["sleep"] = today.isoformat()

    log("HRV")
    hrv = pull_dated(
        token, "hrv.json", "hrv",
        lambda a, b: f"/hrv-service/hrv/daily/{a}/{b}",
        lambda r: (r or {}).get("hrvSummaries"), "calendarDate", since("hrv"), today, 365)
    state["hrv"] = today.isoformat()

    log("Weight")
    weight = pull_dated(
        token, "weight.json", "weight",
        lambda a, b: f"/weight-service/weight/range/{a}/{b}",
        lambda r: (r or {}).get("dailyWeightSummaries"), "summaryDate", since("weight"), today, 365)
    state["weight"] = today.isoformat()

    log("Fitness age and heart rate zones")
    write_json("fitness_age.json", api_get(token, f"/fitnessage-service/fitnessage/v2/{today}") or {})
    write_json("hr_zones.json", api_get(token, "/biometric-service/heartRateZones/v2") or [])

    state["last_pull"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_json("state.json", state)

    def span(d):
        return f"{min(d)} to {max(d)}" if d else "none"

    log("\nDone.")
    log(f"  activities: {len(acts)}"
        + (f" ({acts[-1]['startTimeLocal'][:10]} to {acts[0]['startTimeLocal'][:10]})" if acts else ""))
    log(f"  daily summaries: {len(daily)} days ({span(daily)})")
    log(f"  sleep: {len(sleep)} nights ({span(sleep)})")
    log(f"  hrv: {len(hrv)} nights ({span(hrv)})")
    log(f"  weight: {len(weight)} weigh-ins ({span(weight)})")
    log(f"  saved to {DATA}")


if __name__ == "__main__":
    main()
