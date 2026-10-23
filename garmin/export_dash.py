#!/usr/bin/env python3
"""Export the dashboard's numbers as JSON row files for the Claude dashboard.

Usage:
  python3 export_dash.py

Writes data/dash/<dataset>.json (gitignored, like the rest of data/).
Each file is an array of row objects, one per dataset on the dashboard.
"""

import json
from datetime import date, datetime, timedelta, timezone

from build import DATA, NOT_TRAINING, SPORT, compute, load

TIMELINE_DAYS = 180


def r1(x):
    return None if x is None else round(x, 1)


def timeline():
    """One row per day: hours per sport, sleep stages and score, HRV, resting HR, Body Battery."""
    acts = load("activities.json", [])
    sleep = load("sleep.json", {})
    hrv = load("hrv.json", {})
    daily = load("daily.json", {})
    today = date.today()
    rows = {}
    for i in range(TIMELINE_DAYS - 1, -1, -1):
        k = (today - timedelta(days=i)).isoformat()
        s, h, dl = sleep.get(k), hrv.get(k, {}), daily.get(k, {})
        bed = None
        if s and s.get("sleepStartTimestampLocal"):
            # Garmin encodes local wall-clock time as an epoch, so read it as UTC.
            t = datetime.fromtimestamp(s["sleepStartTimestampLocal"] / 1000, timezone.utc)
            bed = round(t.hour + t.minute / 60 - (24 if t.hour >= 15 else 0), 2)
        hrs = (lambda key: round((s.get(key) or 0) / 3600, 2) if s else None)
        rows[k] = {
            "date": k, "swim_h": 0, "bike_h": 0, "run_h": 0, "gym_h": 0, "other_h": 0, "sessions": "",
            "sleep_h": hrs("sleepTimeSeconds"), "deep_h": hrs("deepSleepSeconds"), "rem_h": hrs("remSleepSeconds"),
            "light_h": hrs("lightSleepSeconds"), "awake_h": hrs("awakeSleepSeconds"),
            "sleep_score": (((s or {}).get("sleepScores") or {}).get("overall") or {}).get("value"),
            "bedtime_h": bed,
            "hrv_ms": h.get("lastNightAvg"), "hrv_low": (h.get("baseline") or {}).get("balancedLow"),
            "hrv_high": (h.get("baseline") or {}).get("balancedUpper"),
            "resting_hr": (dl.get("heartRate") or {}).get("restingValue"),
            "body_battery": (dl.get("bodyBattery") or {}).get("maxValue"),
        }
    col = {"Swim": "swim_h", "Bike": "bike_h", "Run": "run_h", "Strength": "gym_h"}
    for a in sorted(acts, key=lambda a: a["startTimeLocal"]):
        k, t = a["startTimeLocal"][:10], a["activityType"]["typeKey"]
        if k not in rows or t in NOT_TRAINING:
            continue
        r = rows[k]
        h = (a.get("duration") or 0) / 3600
        c = col.get(SPORT.get(t), "other_h")
        r[c] = round(r[c] + h, 2)
        km = (a.get("distance") or 0) / 1000
        label = t.replace("_", " ").capitalize() + f" {round(h * 60)} min" + (f" · {km:.1f} km" if km > 0.05 else "")
        r["sessions"] = f"{r['sessions']}; {label}" if r["sessions"] else label
    return list(rows.values())


def main():
    p = compute()
    out = DATA / "dash"
    out.mkdir(exist_ok=True)

    def session_text(s):
        name = s["type"] if s["sport"] == "Other" else s["sport"]
        km = f" · {s['km']} km" if s["km"] > 0.05 else ""
        return f"{name} {s['min']} min{km}"

    sets = {
        "days": [{"date": d["d"], "train_min": d["min"], "strength": 1 if d["strength"] else 0,
                  "sleep_h": r1(d["sleep"]), "sessions": "; ".join(session_text(s) for s in d["s"])}
                 for d in p["cal"]],
        "timeline": timeline(),
        "sleep_effect": [{"sleep": b["label"], "nights": b["n"], "body_battery": b["bb"], "stress": b["stress"]}
                         for b in p["buckets"]],
        "longest": [{"sport": s, "km": v["km"], "date": v["d"], "best_km": v["all"]}
                    for s, v in p["longest"].items()],
        "zones": [{"zone": f"Zone {i + 1}", "hours": h} for i, h in enumerate(p["zones"])],
    }
    for stale in out.glob("*.json"):
        if stale.stem not in sets:
            stale.unlink()
    for name, rows in sets.items():
        (out / f"{name}.json").write_text(json.dumps(rows, separators=(",", ":")))
        print(f"{name}: {len(rows)} rows")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
