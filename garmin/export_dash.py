#!/usr/bin/env python3
"""Export the dashboard's numbers as JSON row files for the Claude dashboard.

Usage:
  python3 export_dash.py

Writes data/dash/<dataset>.json (gitignored, like the rest of data/).
Each file is an array of row objects, one per dataset on the dashboard.
"""

import json

from build import DATA, compute


def r1(x):
    return None if x is None else round(x, 1)


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
        "recovery": [{"date": r["d"], "sleep_h": r1(r["sleep"]), "bedtime_h": r["bed"], "sleep_score": r["score"],
                      "hrv_ms": r["hrv"], "hrv_low": r["hrvLo"], "hrv_high": r["hrvHi"], "resting_hr": r["rhr"],
                      "body_battery": r["bb"], "stress": r["stress"]} for r in p["rec"]],
        "weeks": [{"week": w["w"], "swim_h": w["Swim"], "bike_h": w["Bike"], "run_h": w["Run"],
                   "strength_sessions": w["strength"], "training_days": w["days"]} for w in p["weeks"]],
        "sleep_effect": [{"sleep": b["label"], "nights": b["n"], "body_battery": b["bb"], "stress": b["stress"]}
                         for b in p["buckets"]],
        "longest": [{"sport": s, "km": v["km"], "date": v["d"], "best_km": v["all"]}
                    for s, v in p["longest"].items()],
        "zones": [{"zone": f"Zone {i + 1}", "hours": h} for i, h in enumerate(p["zones"])],
        "weight": [{"date": w["d"], "kg": w["kg"]} for w in p["weights"]],
        "fitness": [{"lever": l["label"], "now": l["value"], "target": l["target"], "unit": l["unit"]}
                    for l in p["fitAge"]["levers"]]
                   + ([{"lever": "Fitness age", "now": r1(p["fitAge"]["age"]), "target": p["fitAge"]["achievable"],
                        "unit": "years"}] if p["fitAge"]["age"] else []),
    }
    for name, rows in sets.items():
        (out / f"{name}.json").write_text(json.dumps(rows, separators=(",", ":")))
        print(f"{name}: {len(rows)} rows")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
