#!/usr/bin/env python3
"""Build dashboard.html from the JSON in data/.

Usage:
  python3 build.py

Writes one self-contained file (no server, no installs). It holds your
health data, so it is gitignored: keep it on your own devices.
"""

import json
import statistics as st
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "dashboard.html"

# Your targets. Change these and re-run.
WAKE_TARGET = "06:30"
SLEEP_TARGET_H = 7.5          # bedtime target = wake target minus this
SLEEP_FLOOR_H = 7.0           # nights at or above this count as a "good night"
STRENGTH_PER_WEEK = 3
TRAINING_DAYS_PER_WEEK = 5
GOAL = "Ironman"              # long-term race goal
GOAL_DISTANCES = {"Swim": 3.8, "Bike": 180.0, "Run": 42.2}   # km, full Ironman
HALF_DISTANCES = {"Swim": 1.9, "Bike": 90.0, "Run": 21.1}    # km, 70.3

SPORT = {
    "running": "Run", "treadmill_running": "Run", "trail_running": "Run", "track_running": "Run",
    "cycling": "Bike", "indoor_cycling": "Bike", "virtual_ride": "Bike", "road_biking": "Bike",
    "mountain_biking": "Bike", "gravel_cycling": "Bike",
    "lap_swimming": "Swim", "open_water_swimming": "Swim",
    "strength_training": "Strength",
}
NOT_TRAINING = {"walking", "breathwork", "meditation"}


def load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def hm(hours):
    """Clock hours (may be negative = before midnight) -> 'HH:MM'."""
    h = hours % 24
    return f"{int(h):02d}:{int(round((h % 1) * 60)) % 60:02d}"


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.mean(xs) if xs else None


def compute():
    """Everything the dashboard shows, as one dict."""
    acts = load("activities.json", [])
    daily = load("daily.json", {})
    sleep = load("sleep.json", {})
    hrv = load("hrv.json", {})
    weight = load("weight.json", {})
    fit_age = load("fitness_age.json", {})
    state = load("state.json", {})

    today = date.today()
    wake_h = int(WAKE_TARGET[:2]) + int(WAKE_TARGET[3:]) / 60
    bed_target = wake_h - SLEEP_TARGET_H  # e.g. -1.0 = 23:00

    # ---- activities by day -------------------------------------------------
    days = defaultdict(lambda: {"min": 0.0, "sessions": []})
    for a in acts:
        t = a["activityType"]["typeKey"]
        d = a["startTimeLocal"][:10]
        sport = SPORT.get(t, "Other")
        mins = (a.get("duration") or 0) / 60
        km = (a.get("distance") or 0) / 1000
        if t not in NOT_TRAINING:
            days[d]["min"] += mins
        days[d]["sessions"].append({
            "sport": sport, "type": t.replace("_", " "), "name": a.get("activityName") or "",
            "min": round(mins), "km": round(km, 2), "hr": a.get("averageHR"),
            "training": t not in NOT_TRAINING,
        })

    # ---- sleep per night -----------------------------------------------------
    nights = {}
    for d, s in sleep.items():
        hrs = (s.get("sleepTimeSeconds") or 0) / 3600
        bed = None
        if s.get("sleepStartTimestampLocal"):
            # Garmin encodes local wall-clock time as an epoch, so read it as UTC.
            t = datetime.fromtimestamp(s["sleepStartTimestampLocal"] / 1000, timezone.utc)
            bed = t.hour + t.minute / 60
            if bed > 15:
                bed -= 24
        score = ((s.get("sleepScores") or {}).get("overall") or {}).get("value")
        nights[d] = {"h": round(hrs, 2), "bed": None if bed is None else round(bed, 2), "score": score,
                     "deep": round((s.get("deepSleepSeconds") or 0) / 3600, 2),
                     "rem": round((s.get("remSleepSeconds") or 0) / 3600, 2)}

    # ---- calendar: last 53 weeks, Monday-start ------------------------------
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=52)
    cal = []
    d = start
    while d <= today:
        k = d.isoformat()
        info = days.get(k)
        n = nights.get(k)
        cal.append({
            "d": k,
            "min": round(info["min"]) if info else 0,
            "s": [x for x in info["sessions"]] if info else [],
            "sleep": n["h"] if n else None,
            "strength": any(x["sport"] == "Strength" for x in info["sessions"]) if info else False,
        })
        d += timedelta(days=1)

    # ---- weekly rollups (last 26 weeks) --------------------------------------
    week0 = today - timedelta(days=today.weekday())
    weeks = []
    for i in range(25, -1, -1):
        ws = week0 - timedelta(weeks=i)
        hrs = Counter()
        strength = 0
        tdays = 0
        for j in range(7):
            info = days.get((ws + timedelta(days=j)).isoformat())
            if not info:
                continue
            if info["min"] > 0:
                tdays += 1
            for s in info["sessions"]:
                if s["sport"] in ("Swim", "Bike", "Run"):
                    hrs[s["sport"]] += s["min"] / 60
                if s["sport"] == "Strength":
                    strength += 1
        weeks.append({"w": ws.isoformat(), "Swim": round(hrs["Swim"], 2), "Bike": round(hrs["Bike"], 2),
                      "Run": round(hrs["Run"], 2), "strength": strength, "days": tdays})

    # ---- streaks (weeks hitting the training-days target) --------------------
    def week_days(ws):
        return sum(1 for j in range(7) if days.get((ws + timedelta(days=j)).isoformat(), {}).get("min", 0) > 0)

    first = min((date.fromisoformat(a["startTimeLocal"][:10]) for a in acts), default=today)
    ws = first - timedelta(days=first.weekday())
    best = run = 0
    hist = []
    while ws <= week0:
        hit = week_days(ws) >= 3
        hist.append(hit)
        run = run + 1 if hit else 0
        best = max(best, run)
        ws += timedelta(weeks=1)
    # The current week is still in progress: don't let it break the streak yet.
    cur = 0
    for i, hit in enumerate(reversed(hist)):
        if hit:
            cur += 1
        elif i == 0:
            continue
        else:
            break

    year_start = date(today.year, 1, 1).isoformat()
    month_start = today.replace(day=1).isoformat()
    train_days_year = sum(1 for k, v in days.items() if k >= year_start and v["min"] > 0)
    train_days_month = sum(1 for k, v in days.items() if k >= month_start and v["min"] > 0)
    good_nights_30 = sum(1 for k, v in nights.items()
                         if k > (today - timedelta(days=30)).isoformat() and v["h"] >= SLEEP_FLOOR_H)
    nights_30 = sum(1 for k in nights if k > (today - timedelta(days=30)).isoformat())

    # ---- recovery series (last 90 days) --------------------------------------
    since90 = (today - timedelta(days=89)).isoformat()
    rec = []
    for i in range(89, -1, -1):
        k = (today - timedelta(days=i)).isoformat()
        dl = daily.get(k, {})
        hv = hrv.get(k, {})
        n = nights.get(k, {})
        rec.append({
            "d": k,
            "sleep": n.get("h"), "bed": n.get("bed"), "score": n.get("score"),
            "hrv": hv.get("lastNightAvg"),
            "hrvLo": (hv.get("baseline") or {}).get("balancedLow"),
            "hrvHi": (hv.get("baseline") or {}).get("balancedUpper"),
            "rhr": (dl.get("heartRate") or {}).get("restingValue"),
            "bb": (dl.get("bodyBattery") or {}).get("maxValue"),
            "stress": (dl.get("stress") or {}).get("avgLevel"),
        })

    # ---- readiness: today vs your own baseline -------------------------------
    last = next((r for r in reversed(rec) if r["sleep"] is not None or r["hrv"] is not None), {})
    rhr30 = mean([r["rhr"] for r in rec[-31:-1]])
    checks = []
    if last.get("sleep") is not None:
        h = last["sleep"]
        checks.append(("Sleep", f"{h:.1f} h", "good" if h >= SLEEP_FLOOR_H else "warning" if h >= 6 else "critical"))
    if last.get("hrv") is not None and last.get("hrvLo"):
        v = last["hrv"]
        checks.append(("HRV", f"{v} ms (normal {last['hrvLo']}–{last['hrvHi']})",
                       "good" if v >= last["hrvLo"] else "warning" if v >= last["hrvLo"] - 6 else "critical"))
    if last.get("rhr") is not None and rhr30:
        diff = last["rhr"] - rhr30
        checks.append(("Resting HR", f"{last['rhr']} bpm ({diff:+.0f} vs 30-day avg)",
                       "good" if diff <= 2 else "warning" if diff <= 5 else "critical"))
    if last.get("bb") is not None:
        b = last["bb"]
        checks.append(("Body Battery", f"{b} peak", "good" if b >= 75 else "warning" if b >= 50 else "critical"))
    levels = [c[2] for c in checks]
    if "critical" in levels or levels.count("warning") >= 3:
        verdict = ("critical", "Recover", "Rest or an easy 20–30 min. Prioritise an early night.")
    elif "warning" in levels:
        verdict = ("warning", "Keep it easy", "Zone 2 or technique work. Save the hard session.")
    else:
        verdict = ("good", "Go hard", "Recovered. Good day for intervals or a heavy lift.")

    # ---- sleep -> next day (all history) --------------------------------------
    buckets = []
    for lo, hi, lab in [(0, 6, "Under 6 h"), (6, 7, "6–7 h"), (7, 8, "7–8 h"), (8, 24, "8 h +")]:
        bb, stx = [], []
        for k, n in nights.items():
            if lo <= n["h"] < hi:
                dl = daily.get(k, {})
                bb.append((dl.get("bodyBattery") or {}).get("maxValue"))
                s = (dl.get("stress") or {}).get("avgLevel")
                stx.append(s if s and s > 0 else None)
        cnt = sum(1 for n in nights.values() if lo <= n["h"] < hi)
        buckets.append({"label": lab, "n": cnt,
                        "bb": round(mean(bb)) if mean(bb) else None,
                        "stress": round(mean(stx)) if mean(stx) else None})

    beds = [n["bed"] for k, n in nights.items() if k >= since90 and n["bed"] is not None]
    bed_med = st.median(beds) if beds else None
    bed_sd = st.pstdev(beds) if len(beds) > 1 else None
    on_time = sum(1 for b in beds if b <= bed_target + 0.25)

    # ---- endurance: longest in last 12 weeks + HR zones (last 8 weeks) ------
    since12w = (today - timedelta(weeks=12)).isoformat()
    longest = {}
    for sport in ("Swim", "Bike", "Run"):
        best_km, best_d = 0, None
        for a in acts:
            if SPORT.get(a["activityType"]["typeKey"]) == sport and a["startTimeLocal"][:10] >= since12w:
                km = (a.get("distance") or 0) / 1000
                if km > best_km:
                    best_km, best_d = km, a["startTimeLocal"][:10]
        alltime = max(((a.get("distance") or 0) / 1000 for a in acts
                       if SPORT.get(a["activityType"]["typeKey"]) == sport), default=0)
        longest[sport] = {"km": round(best_km, 2), "d": best_d, "all": round(alltime, 2),
                          "half": HALF_DISTANCES[sport], "full": GOAL_DISTANCES[sport]}

    since8w = (today - timedelta(weeks=8)).isoformat()
    zones = [0.0] * 5
    for a in acts:
        if a["startTimeLocal"][:10] >= since8w and a["activityType"]["typeKey"] not in NOT_TRAINING:
            for z in range(5):
                zones[z] += (a.get(f"hrTimeInZone_{z + 1}") or 0) / 3600

    # ---- body ---------------------------------------------------------------
    weights = [{"d": k, "kg": round(v["latestWeight"]["weight"] / 1000, 1)}
               for k, v in sorted(weight.items()) if (v.get("latestWeight") or {}).get("weight")]
    comps = fit_age.get("components") or {}
    levers = []
    for key, label, unit in [("vigorousMinutesAvg", "Vigorous minutes / week", "min"),
                             ("vigorousDaysAvg", "Vigorous days / week", "days"),
                             ("bmi", "BMI", ""), ("rhr", "Resting HR", "bpm")]:
        c = comps.get(key)
        if c and c.get("value") is not None:
            levers.append({"label": label, "value": round(c["value"], 1), "target": c.get("targetValue"),
                           "unit": unit, "lower": key in ("bmi", "rhr")})

    payload = {
        "generated": datetime.now().strftime("%d %b %Y, %H:%M"),
        "lastPull": state.get("last_pull", ""),
        "goal": GOAL,
        "targets": {"wake": WAKE_TARGET, "bed": hm(bed_target), "bedH": bed_target, "sleepH": SLEEP_TARGET_H,
                    "floorH": SLEEP_FLOOR_H, "strength": STRENGTH_PER_WEEK, "days": TRAINING_DAYS_PER_WEEK},
        "verdict": verdict, "checks": checks, "lastDate": last.get("d"),
        "streak": {"current": cur, "best": best, "yearDays": train_days_year, "monthDays": train_days_month,
                   "goodNights": good_nights_30, "nights": nights_30},
        "cal": cal, "weeks": weeks, "rec": rec, "buckets": buckets,
        "bed": {"median": bed_med, "sd": bed_sd, "onTime": on_time, "n": len(beds)},
        "longest": longest, "zones": [round(z, 2) for z in zones],
        "weights": weights, "fitAge": {"age": fit_age.get("fitnessAge"), "real": fit_age.get("chronologicalAge"),
                                       "achievable": fit_age.get("achievableFitnessAge"), "levers": levers},
    }
    return payload


def main():
    payload = compute()
    html = TEMPLATE.replace("/*DATA*/null", json.dumps(payload, separators=(",", ":")))
    OUT.write_text(html)
    print(f"Wrote {OUT}")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training & Recovery</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s7: #6250d6;
  --q0: #ecebe6; --q1: #b7d3f6; --q2: #86b6ef; --q3: #3987e5; --q4: #1c5cab; --q5: #0d366b;
  --good: #0ca30c; --warning: #fab219; --critical: #d03b3b; --good-ink: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s7: #9085e9;
    --q0: #262624; --q1: #104281; --q2: #1c5cab; --q3: #2a78d6; --q4: #5598e7; --q5: #9ec5f4;
    --good-ink: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s7: #9085e9;
  --q0: #262624; --q1: #104281; --q2: #1c5cab; --q3: #2a78d6; --q4: #5598e7; --q5: #9ec5f4;
  --good-ink: #0ca30c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 24px 16px 64px; }
header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 24px; margin: 0; letter-spacing: -0.01em; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-2); margin: 40px 0 12px; }
h3 { font-size: 15px; margin: 0 0 2px; }
.sub { color: var(--muted); font-size: 13px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 16px; }
.grid { display: grid; gap: 12px; }
.g2 { grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); }
.g3 { grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); }
.g4 { grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
.tile .v { font-size: 28px; font-weight: 600; line-height: 1.1; margin-top: 4px; }
.tile .l { font-size: 13px; color: var(--ink-2); }
.tile .n { font-size: 12px; color: var(--muted); margin-top: 2px; }
.verdict { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
.badge { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; font-size: 20px; }
.dot { width: 14px; height: 14px; border-radius: 50%; flex: none; display: inline-block; }
.checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 8px 16px; margin-top: 12px; }
.check { display: flex; gap: 8px; align-items: center; font-size: 14px; }
.check b { font-weight: 600; }
.tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.tabs button { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--ring);
  background: transparent; color: var(--ink-2); cursor: pointer; }
.tabs button[aria-pressed="true"] { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.calwrap { overflow-x: auto; padding-bottom: 4px; }
svg { display: block; overflow: visible; }
svg text { fill: var(--muted); font-size: 11px; font-family: inherit; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2); margin-top: 8px; align-items: center; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.bar-row { display: grid; grid-template-columns: 56px 1fr; gap: 10px; align-items: center; margin: 12px 0; font-size: 13px; }
.track { position: relative; height: 10px; background: var(--q0); border-radius: 4px; }
.fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 4px; background: var(--s1); }
.tick { position: absolute; top: -4px; bottom: -4px; width: 2px; background: var(--ink-2); }
.bar-meta { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); margin-top: 4px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--grid); }
th { color: var(--ink-2); font-weight: 500; }
#tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--surface); font-size: 12px;
  padding: 6px 8px; border-radius: 6px; max-width: 260px; opacity: 0; transition: opacity .1s; z-index: 10; line-height: 1.4; }
.hit { cursor: default; }
.note { font-size: 13px; color: var(--ink-2); margin: 8px 0 0; }
footer { margin-top: 40px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
  <header>
    <div><h1>Training &amp; Recovery</h1><div class="sub" id="goal"></div></div>
    <div class="sub" id="updated"></div>
  </header>

  <h2>Today</h2>
  <div class="card" id="today"></div>

  <h2>Consistency</h2>
  <div class="grid g4" id="streaks"></div>
  <div class="card" style="margin-top:12px">
    <div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">
      <div><h3 id="calTitle">Every day you trained</h3><div class="sub" id="calSub"></div></div>
      <div class="tabs" role="group" aria-label="Calendar view">
        <button data-view="train" aria-pressed="true">Training</button>
        <button data-view="strength" aria-pressed="false">Strength</button>
        <button data-view="sleep" aria-pressed="false">Sleep</button>
      </div>
    </div>
    <div class="calwrap"><svg id="cal"></svg></div>
    <div class="legend" id="calLegend"></div>
  </div>

  <h2>Recover</h2>
  <div class="grid g2">
    <div class="card"><h3>Sleep per night</h3><div class="sub" id="sleepSub"></div><svg id="sleepChart"></svg></div>
    <div class="card"><h3>Bedtime</h3><div class="sub" id="bedSub"></div><svg id="bedChart"></svg></div>
    <div class="card"><h3>HRV</h3><div class="sub">Overnight average (ms); shaded band is your normal range</div><svg id="hrvChart"></svg></div>
    <div class="card"><h3>Resting heart rate</h3><div class="sub">bpm; lower usually means better recovered</div><svg id="rhrChart"></svg></div>
  </div>
  <div class="card" style="margin-top:12px">
    <h3>What a night's sleep does to your next day</h3>
    <div class="sub">All your history. Body Battery peak and average stress the day after.</div>
    <table id="buckets" style="margin-top:8px"></table>
  </div>

  <h2>Build</h2>
  <div class="grid g2">
    <div class="card"><h3>Strength sessions per week</h3><div class="sub" id="strSub"></div><svg id="strChart"></svg></div>
    <div class="card"><h3>Body</h3><div class="sub">Weigh-ins and the fitness-age levers Garmin tracks</div><div id="body"></div></div>
  </div>

  <h2>Endure</h2>
  <div class="grid g3">
    <div class="card"><h3>Swim, bike, run hours per week</h3><div class="sub">Last 26 weeks</div><svg id="volChart"></svg>
      <div class="legend"><span><i class="sw" style="background:var(--s1)"></i>Swim</span><span><i class="sw" style="background:var(--s2)"></i>Bike</span><span><i class="sw" style="background:var(--s3)"></i>Run</span></div></div>
    <div class="card"><h3>Longest session vs race distance</h3><div class="sub">Longest in the last 12 weeks. Tick marks: 70.3 and full distance</div><div id="longest"></div></div>
    <div class="card"><h3>Heart-rate zones</h3><div class="sub">Time in each zone, last 8 weeks. Base building lives in zones 1–2</div><svg id="zoneChart"></svg></div>
  </div>

  <footer id="foot"></footer>
</main>
<div id="tip" role="tooltip"></div>
<script>
const D = /*DATA*/null;
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const fmtD = s => new Date(s + "T00:00").toLocaleDateString(undefined, {day: "numeric", month: "short"});
const fmtDL = s => new Date(s + "T00:00").toLocaleDateString(undefined, {weekday: "short", day: "numeric", month: "short", year: "numeric"});
const clock = h => { if (h == null) return "–"; const x = ((h % 24) + 24) % 24; const m = Math.round((x % 1) * 60) % 60; return String(Math.floor(x)).padStart(2, "0") + ":" + String(m).padStart(2, "0"); };
const status = {good: "var(--good)", warning: "var(--warning)", critical: "var(--critical)"};
const icon = {good: "✓", warning: "!", critical: "✕"};

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
const tip = $("tip");
function bindTip(node, html) {
  node.classList.add("hit");
  node.addEventListener("pointerenter", () => { tip.innerHTML = html; tip.style.opacity = 1; });
  node.addEventListener("pointermove", e => {
    const w = tip.offsetWidth, x = e.clientX + 12 + w > innerWidth ? e.clientX - w - 12 : e.clientX + 12;
    tip.style.left = x + "px"; tip.style.top = (e.clientY + 14) + "px";
  });
  node.addEventListener("pointerleave", () => { tip.style.opacity = 0; });
}
function width(svg) { return Math.max(280, svg.parentElement.clientWidth - 32); }

// ---------- header ----------
$("goal").textContent = "Goal: " + D.goal + ", built strong, recovered and sharp";
$("updated").textContent = "Data pulled " + (D.lastPull ? new Date(D.lastPull).toLocaleString() : "–");
$("foot").textContent = "Built " + D.generated + ". Run ./refresh.sh to pull new data and rebuild. Targets live at the top of build.py.";

// ---------- today ----------
(() => {
  const [lvl, label, advice] = D.verdict;
  $("today").innerHTML =
    `<div class="verdict"><span class="badge"><span class="dot" style="background:${status[lvl]}"></span>${icon[lvl]} ${label}</span>
     <span class="sub">${advice}${D.lastDate ? " Based on " + fmtDL(D.lastDate) + "." : ""}</span></div>
     <div class="checks">${D.checks.map(([n, v, l]) =>
       `<div class="check"><span class="dot" style="background:${status[l]};width:10px;height:10px"></span><span><b>${n}</b> ${v}</span></div>`).join("")}</div>
     <p class="note">Targets: in bed by <b>${D.targets.bed}</b> to wake at <b>${D.targets.wake}</b> with ${D.targets.sleepH} h of sleep.</p>`;
})();

// ---------- streak tiles ----------
(() => {
  const s = D.streak;
  const tiles = [
    ["Current streak", s.current + (s.current === 1 ? " week" : " weeks"), "Weeks with 3+ training days"],
    ["Best streak", s.best + (s.best === 1 ? " week" : " weeks"), "Your record so far"],
    ["Training days", s.monthDays, "This month · " + s.yearDays + " this year"],
    ["Good nights", s.goodNights + " / " + s.nights, D.targets.floorH + " h+ in the last 30 days"],
  ];
  $("streaks").innerHTML = tiles.map(([l, v, n]) => `<div class="card tile"><div class="l">${l}</div><div class="v">${v}</div><div class="n">${n}</div></div>`).join("");
})();

// ---------- calendar ----------
const calViews = {
  train: {
    title: "Every day you trained",
    sub: d => d.filter(x => x.min > 0).length + " training days in the last 12 months. Darker = longer.",
    level: x => x.min <= 0 ? 0 : x.min < 30 ? 1 : x.min < 60 ? 2 : x.min < 90 ? 3 : x.min < 150 ? 4 : 5,
    legend: ["Rest", "< 30 min", "30–60", "60–90", "90–150", "150 +"],
  },
  strength: {
    title: "Every strength session",
    sub: d => d.filter(x => x.strength).length + " strength days in the last 12 months. Target " + D.targets.strength + " a week.",
    level: x => x.strength ? 4 : x.min > 0 ? 1 : 0,
    legend: ["Rest", "Other training", null, null, "Strength", null],
  },
  sleep: {
    title: "Every night you slept",
    sub: d => d.filter(x => x.sleep >= D.targets.floorH).length + " nights of " + D.targets.floorH + " h or more in the last 12 months.",
    level: x => x.sleep == null ? 0 : x.sleep < 5 ? 1 : x.sleep < 6 ? 2 : x.sleep < 7 ? 3 : x.sleep < 8 ? 4 : 5,
    legend: ["No data", "< 5 h", "5–6 h", "6–7 h", "7–8 h", "8 h +"],
  },
};
function drawCal(view) {
  const V = calViews[view], svg = $("cal");
  svg.innerHTML = "";
  const cell = 13, gap = 3, step = cell + gap, left = 28, top = 18;
  const nW = Math.ceil(D.cal.length / 7);
  svg.setAttribute("width", left + nW * step); svg.setAttribute("height", top + 7 * step);
  ["Mon", "", "Wed", "", "Fri", "", "Sun"].forEach((t, i) => { if (t) el("text", {x: 0, y: top + i * step + 10}, svg).textContent = t; });
  let lastMonth = -1;
  D.cal.forEach((x, i) => {
    const w = Math.floor(i / 7), dw = i % 7, dt = new Date(x.d + "T00:00");
    if (dw === 0 && dt.getMonth() !== lastMonth && dt.getDate() <= 7) {
      lastMonth = dt.getMonth();
      el("text", {x: left + w * step, y: 10}, svg).textContent = dt.toLocaleDateString(undefined, {month: "short"});
    }
    const lv = V.level(x);
    const r = el("rect", {x: left + w * step, y: top + dw * step, width: cell, height: cell, rx: 3, fill: `var(--q${lv})`}, svg);
    let html = `<b>${fmtDL(x.d)}</b>`;
    if (view === "sleep") html += `<br>${x.sleep == null ? "No sleep data" : x.sleep.toFixed(1) + " h sleep"}`;
    else if (!x.s.length) html += "<br>Rest day";
    else html += x.s.map(s => `<br>${s.sport === "Other" ? s.type : s.sport}: ${s.min} min${s.km > 0.05 ? " · " + s.km + " km" : ""}`).join("");
    bindTip(r, html);
  });
  $("calTitle").textContent = V.title;
  $("calSub").textContent = V.sub(D.cal);
  svg.parentElement.scrollLeft = svg.parentElement.scrollWidth;  // open on the latest weeks
  $("calLegend").innerHTML = V.legend.map((t, i) => t ? `<span><i class="sw" style="background:var(--q${i})"></i>${t}</span>` : "").join("");
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach(o => o.setAttribute("aria-pressed", o === b));
  drawCal(b.dataset.view);
}));

// ---------- generic charts ----------
function frame(svg, h, yMin, yMax, ticks, fmt) {
  svg.innerHTML = "";
  const W = width(svg), m = {l: 36, r: 8, t: 10, b: 22};
  svg.setAttribute("width", W); svg.setAttribute("height", h);
  const y = v => m.t + (h - m.t - m.b) * (1 - (v - yMin) / (yMax - yMin));
  ticks.forEach(t => {
    el("line", {x1: m.l, x2: W - m.r, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1}, svg);
    el("text", {x: m.l - 6, y: y(t) + 4, "text-anchor": "end"}, svg).textContent = fmt ? fmt(t) : t;
  });
  return {W, m, y, h};
}
function xLabels(svg, f, items, xOf, every) {
  items.forEach((it, i) => { if (i % every === 0) el("text", {x: xOf(i), y: f.h - 4, "text-anchor": "middle"}, svg).textContent = fmtD(it.d || it.w); });
}

function lineChart(svgId, rows, key, opts) {
  const svg = $(svgId), vals = rows.map(r => r[key]).filter(v => v != null);
  if (!vals.length) { svg.outerHTML = '<p class="note">No data yet.</p>'; return; }
  let lo = Math.min(...vals, ...(opts.band ? rows.map(r => r[opts.band[0]]).filter(v => v != null) : []));
  let hi = Math.max(...vals, ...(opts.band ? rows.map(r => r[opts.band[1]]).filter(v => v != null) : []));
  lo = Math.floor((lo - 3) / 5) * 5; hi = Math.ceil((hi + 3) / 5) * 5;
  const tk = []; for (let t = lo; t <= hi; t += Math.max(5, Math.round((hi - lo) / 4 / 5) * 5)) tk.push(t);
  const f = frame(svg, 180, lo, hi, tk);
  const x = i => f.m.l + (f.W - f.m.l - f.m.r) * i / (rows.length - 1);
  if (opts.band) {
    const pts = rows.map((r, i) => r[opts.band[1]] != null ? [x(i), f.y(r[opts.band[1]])] : null).filter(Boolean);
    const bot = rows.map((r, i) => r[opts.band[0]] != null ? [x(i), f.y(r[opts.band[0]])] : null).filter(Boolean).reverse();
    if (pts.length) el("path", {d: "M" + pts.concat(bot).map(p => p.join(",")).join("L") + "Z", fill: "var(--s1)", opacity: .12}, svg);
  }
  let d = "", pen = false;
  rows.forEach((r, i) => { if (r[key] == null) { pen = false; return; } d += (pen ? "L" : "M") + x(i) + "," + f.y(r[key]); pen = true; });
  el("path", {d, fill: "none", stroke: "var(--s1)", "stroke-width": 2, "stroke-linejoin": "round"}, svg);
  xLabels(svg, f, rows, x, 30);
  // crosshair
  const ch = el("line", {y1: f.m.t, y2: f.h - f.m.b, stroke: "var(--axis)", "stroke-width": 1, opacity: 0}, svg);
  const dot = el("circle", {r: 4, fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2, opacity: 0}, svg);
  const ov = el("rect", {x: f.m.l, y: 0, width: f.W - f.m.l - f.m.r, height: f.h, fill: "transparent"}, svg);
  ov.addEventListener("pointermove", e => {
    const bx = svg.getBoundingClientRect(), i = Math.round((e.clientX - bx.left - f.m.l) / (f.W - f.m.l - f.m.r) * (rows.length - 1));
    const r = rows[Math.max(0, Math.min(rows.length - 1, i))], xi = x(rows.indexOf(r));
    ch.setAttribute("x1", xi); ch.setAttribute("x2", xi); ch.setAttribute("opacity", 1);
    if (r[key] != null) { dot.setAttribute("cx", xi); dot.setAttribute("cy", f.y(r[key])); dot.setAttribute("opacity", 1); } else dot.setAttribute("opacity", 0);
    tip.innerHTML = `<b>${fmtDL(r.d)}</b><br>${r[key] == null ? "No reading" : r[key] + " " + opts.unit}` +
      (opts.band && r[opts.band[0]] != null ? `<br>Normal ${r[opts.band[0]]}–${r[opts.band[1]]}` : "");
    tip.style.opacity = 1; tip.style.left = (e.clientX + 12) + "px"; tip.style.top = (e.clientY + 14) + "px";
  });
  ov.addEventListener("pointerleave", () => { ch.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); tip.style.opacity = 0; });
}

function barChart(svgId, rows, valOf, opts) {
  const svg = $(svgId), max = Math.max(opts.target || 0, ...rows.map(valOf), 1);
  const hi = opts.hi || Math.ceil(max);
  const tk = opts.ticks || [0, Math.round(hi / 2 * 10) / 10, hi];
  const f = frame(svg, opts.h || 180, 0, hi, tk, opts.fmt);
  const n = rows.length, bw = (f.W - f.m.l - f.m.r) / n, x = i => f.m.l + bw * i + bw / 2;
  rows.forEach((r, i) => {
    let base = 0;
    (opts.stack || [[null, valOf, opts.color || "var(--s1)"]]).forEach(([name, fn, color]) => {
      const v = fn(r); if (!v) return;
      const y0 = f.y(base), y1 = f.y(base + v);
      el("rect", {x: f.m.l + bw * i + 1, y: y1 + (base ? 1 : 0), width: Math.max(1, bw - 2), height: Math.max(1, y0 - y1 - (base ? 1 : 0)), rx: base ? 0 : 2, fill: color}, svg);
      base += v;
    });
    if (opts.colorOf) { /* per-bar color handled by stack */ }
    const hit = el("rect", {x: f.m.l + bw * i, y: f.m.t, width: bw, height: f.h - f.m.t - f.m.b, fill: "transparent"}, svg);
    bindTip(hit, opts.tip(r));
  });
  if (opts.target) {
    el("line", {x1: f.m.l, x2: f.W - f.m.r, y1: f.y(opts.target), y2: f.y(opts.target), stroke: "var(--ink-2)", "stroke-width": 1.5, "stroke-dasharray": "4 3"}, svg);
    el("text", {x: f.W - f.m.r, y: f.y(opts.target) - 4, "text-anchor": "end", style: "fill:var(--ink-2)"}, svg).textContent = opts.targetLabel;
  }
  xLabels(svg, f, rows, x, opts.every || 8);
}

// ---------- recover ----------
(() => {
  const R = D.rec.slice(-60), T = D.targets;
  const good = R.filter(r => r.sleep >= T.floorH).length, have = R.filter(r => r.sleep != null).length;
  $("sleepSub").textContent = `Last 60 nights: ${good} of ${have} reached ${T.floorH} h. Dashed line: ${T.sleepH} h target.`;
  barChart("sleepChart", R, r => r.sleep || 0, {
    hi: 10, ticks: [0, 4, 7, 10], fmt: t => t + "h", target: T.sleepH, targetLabel: T.sleepH + " h", every: 15,
    stack: [[null, r => r.sleep || 0, "var(--s7)"]],
    tip: r => `<b>${fmtDL(r.d)}</b><br>` + (r.sleep == null ? "No sleep data" : `${r.sleep.toFixed(1)} h sleep${r.score ? " · score " + r.score : ""}<br>Asleep ${clock(r.bed)}`),
  });

  // bedtime dot plot
  const svg = $("bedChart"), B = D.bed;
  $("bedSub").textContent = B.median == null ? "No data" :
    `Median ${clock(B.median)}, varies ±${(B.sd * 60).toFixed(0)} min. On time (by ${clock(T.bedH + .25)}) ${B.onTime} of ${B.n} nights in 90 days.`;
  const lo = -3, hi = 5; // 21:00 .. 05:00
  const f = frame(svg, 180, lo, hi, [-3, -1, 1, 3, 5], t => clock(t));
  // flip so earlier is higher: remap y
  const y = v => f.m.t + (f.h - f.m.t - f.m.b) * ((v - lo) / (hi - lo));
  svg.querySelectorAll("line,text").forEach(n => n.remove());
  [-3, -1, 1, 3, 5].forEach(t => {
    el("line", {x1: f.m.l, x2: f.W - f.m.r, y1: y(t), y2: y(t), stroke: "var(--grid)"}, svg);
    el("text", {x: f.m.l - 6, y: y(t) + 4, "text-anchor": "end"}, svg).textContent = clock(t);
  });
  el("rect", {x: f.m.l, width: f.W - f.m.l - f.m.r, y: y(T.bedH - .5), height: y(T.bedH + .25) - y(T.bedH - .5), fill: "var(--good)", opacity: .15}, svg);
  el("text", {x: f.W - f.m.r, y: y(T.bedH - .5) - 4, "text-anchor": "end", style: "fill:var(--ink-2)"}, svg).textContent = "Target " + T.bed;
  const x = i => f.m.l + (f.W - f.m.l - f.m.r) * (i + .5) / R.length;
  R.forEach((r, i) => {
    if (r.bed == null) return;
    const v = Math.max(lo, Math.min(hi, r.bed));
    const c = el("circle", {cx: x(i), cy: y(v), r: 4, fill: "var(--s7)", stroke: "var(--surface)", "stroke-width": 2}, svg);
    const hit = el("circle", {cx: x(i), cy: y(v), r: 9, fill: "transparent"}, svg);
    bindTip(hit, `<b>${fmtDL(r.d)}</b><br>Asleep ${clock(r.bed)} · ${r.sleep ? r.sleep.toFixed(1) + " h" : ""}`);
  });
  xLabels(svg, f, R, x, 15);

  lineChart("hrvChart", D.rec, "hrv", {unit: "ms", band: ["hrvLo", "hrvHi"]});
  lineChart("rhrChart", D.rec, "rhr", {unit: "bpm"});

  $("buckets").innerHTML = "<tr><th>Sleep</th><th>Nights</th><th>Body Battery peak</th><th>Avg stress</th></tr>" +
    D.buckets.map(b => `<tr><td>${b.label}</td><td>${b.n}</td><td>${b.bb ?? "–"}</td><td>${b.stress ?? "–"}</td></tr>`).join("");
})();

// ---------- build ----------
(() => {
  const W = D.weeks, T = D.targets;
  const hit = W.filter(w => w.strength >= T.strength).length;
  $("strSub").textContent = `Weeks at ${T.strength}+ sessions: ${hit} of the last ${W.length}. Start a Strength activity on your watch each gym session so it counts.`;
  barChart("strChart", W, w => w.strength, {
    hi: Math.max(4, T.strength + 1), ticks: [0, 1, 2, 3, 4].filter(t => t <= Math.max(4, T.strength + 1)), target: T.strength,
    targetLabel: "Target " + T.strength, stack: [[null, w => w.strength, "var(--s2)"]],
    tip: w => `<b>Week of ${fmtD(w.w)}</b><br>${w.strength} strength session${w.strength === 1 ? "" : "s"}<br>${w.days} training days`,
  });

  const fa = D.fitAge, wt = D.weights;
  let html = "";
  if (wt.length) {
    const lastW = wt[wt.length - 1], firstW = wt[0];
    html += `<div class="grid g4" style="margin-top:12px"><div class="tile"><div class="l">Weight</div><div class="v">${lastW.kg} kg</div><div class="n">${fmtD(lastW.d)} · ${(lastW.kg - firstW.kg >= 0 ? "+" : "") + (lastW.kg - firstW.kg).toFixed(1)} kg since ${fmtD(firstW.d)}</div></div>`;
    if (fa.age) html += `<div class="tile"><div class="l">Fitness age</div><div class="v">${fa.age.toFixed(1)}</div><div class="n">Real age ${fa.real} · achievable ${fa.achievable}</div></div>`;
    html += "</div>";
    if (wt.length < 8) html += `<p class="note">Only ${wt.length} weigh-ins. Weighing once a week, same time, makes this trend meaningful.</p>`;
  }
  if (fa.levers.length) {
    html += `<table style="margin-top:12px"><tr><th>Lever</th><th>Now</th><th>Target</th></tr>` +
      fa.levers.map(l => {
        const ok = l.target == null || (l.lower ? l.value <= l.target : l.value >= l.target);
        return `<tr><td>${l.label}</td><td>${l.value}${l.unit ? " " + l.unit : ""}</td><td>${l.target == null ? "–" : (ok ? "✓ " : "") + l.target}</td></tr>`;
      }).join("") + "</table>";
  }
  $("body").innerHTML = html || '<p class="note">No body data yet.</p>';
})();

// ---------- endure ----------
(() => {
  const W = D.weeks;
  const max = Math.max(1, ...W.map(w => w.Swim + w.Bike + w.Run));
  const hi = Math.ceil(max);
  barChart("volChart", W, w => w.Swim + w.Bike + w.Run, {
    hi, ticks: [0, Math.round(hi / 2), hi].filter((v, i, a) => a.indexOf(v) === i), fmt: t => t + "h",
    stack: [["Swim", w => w.Swim, "var(--s1)"], ["Bike", w => w.Bike, "var(--s2)"], ["Run", w => w.Run, "var(--s3)"]],
    tip: w => `<b>Week of ${fmtD(w.w)}</b><br>Swim ${w.Swim.toFixed(1)} h<br>Bike ${w.Bike.toFixed(1)} h<br>Run ${w.Run.toFixed(1)} h`,
  });

  const L = D.longest;
  $("longest").innerHTML = ["Swim", "Bike", "Run"].map(s => {
    const x = L[s], pct = Math.min(100, x.km / x.full * 100), half = x.half / x.full * 100;
    return `<div class="bar-row"><b>${s}</b><div><div class="track"><div class="fill" style="width:${pct}%"></div>
      <div class="tick" style="left:calc(${half}% - 1px)" title="70.3"></div><div class="tick" style="left:calc(100% - 2px)" title="Full"></div></div>
      <div class="bar-meta"><span>${x.km} km${x.d ? " · " + fmtD(x.d) : " · none in 12 weeks"}</span><span>70.3 ${x.half} · full ${x.full} km</span></div></div></div>`;
  }).join("") + `<p class="note">Best ever in your data: swim ${L.Swim.all} km, bike ${L.Bike.all} km, run ${L.Run.all} km.</p>`;

  const Z = D.zones, svg = $("zoneChart"), total = Z.reduce((a, b) => a + b, 0);
  if (!total) { svg.outerHTML = '<p class="note">No heart-rate zone data in the last 8 weeks.</p>'; return; }
  const W2 = width(svg), rowH = 26;
  svg.setAttribute("width", W2); svg.setAttribute("height", Z.length * rowH);
  const zc = ["var(--q1)", "var(--q2)", "var(--q3)", "var(--q4)", "var(--q5)"];
  const zmax = Math.max(...Z);
  Z.forEach((h, i) => {
    const y = i * rowH, bw = (W2 - 120) * h / zmax;
    el("text", {x: 0, y: y + 15}, svg).textContent = "Zone " + (i + 1);
    el("rect", {x: 56, y: y + 4, width: Math.max(2, bw), height: 14, rx: 3, fill: zc[i]}, svg);
    el("text", {x: 62 + bw, y: y + 15, style: "fill:var(--ink-2)"}, svg).textContent = h.toFixed(1) + " h · " + Math.round(h / total * 100) + "%";
    const hit = el("rect", {x: 0, y, width: W2, height: rowH, fill: "transparent"}, svg);
    bindTip(hit, `Zone ${i + 1}: ${h.toFixed(1)} h (${Math.round(h / total * 100)}% of training time)`);
  });
})();

drawCal("train");
// Charts are sized to their card; redraw on a real width change (not mobile toolbar scroll).
let rt, lastW = innerWidth;
addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (Math.abs(innerWidth - lastW) > 40) location.reload(); }, 300); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
