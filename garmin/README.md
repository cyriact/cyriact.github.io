# Garmin data pull

Pulls your training and health history from Garmin's official, read-only
Personal API into `data/` as JSON. Python 3.10+ standard library only.

## Setup

1. Create a key at <https://connect.garmin.com/app/settings/personalApiToken>
   (Create Key, accept terms, 1 year expiry). Garmin shows it once.
2. Create `garmin/.env.local` with one line:

   ```
   GARMIN_PERSONAL_API_KEY=<your key>
   ```

   `.env.local` and `data/` are gitignored. This repo is a public GitHub Pages
   site, so never commit either one.

## Pull

```sh
./refresh.sh            # last 12 months on first run, then only what's new
./refresh.sh --full     # full history, back to your oldest activity
./refresh.sh --days 90  # smaller first-run window
```

`refresh.sh` also rebuilds the dashboard. To rebuild without pulling: `python3 build.py`.

## Dashboard

`build.py` writes `dashboard.html`, one self-contained file that opens in any
browser (no server). It is gitignored because it contains your health data.

- **Today**: a go-hard / keep-it-easy / recover call from last night's sleep,
  HRV against your normal range, resting HR against your 30-day average, and Body Battery.
- **Consistency**: weekly streaks plus a year calendar of every training day,
  strength session and night's sleep.
- **Recover**: sleep per night, bedtime against target, HRV, resting HR, and how
  your sleep length shows up in the next day's Body Battery and stress.
- **Build**: strength sessions per week, weight, fitness-age levers.
- **Endure**: swim/bike/run hours per week, longest session vs 70.3 and full
  Ironman distances, time in heart-rate zones.

Targets (wake time, sleep hours, strength sessions per week) are constants at
the top of `build.py`.

## What lands in `data/`

| File | Contents |
| --- | --- |
| `activities.json` | Every activity: distance (m), duration (s), HR, zones, training load |
| `daily.json` | Per day: steps, resting HR, stress, Body Battery, intensity minutes |
| `sleep.json` | Per night: stages, sleep score |
| `hrv.json` | Per night: last-night avg, weekly avg, baseline, status |
| `weight.json` | Weigh-ins (`latestWeight.weight` is in grams) |
| `fitness_age.json`, `hr_zones.json` | Latest snapshot |
| `token.json`, `state.json` | 24 h access token cache and sync bookmarks |

## API notes

- The key is exchanged for a 24 h bearer token (cached, renewed after 20 h).
- Responses over ~256 KB fail with HTTP 500; the script halves the date range
  and retries automatically.
- Missing nights or days are normal (watch off, no HRV on older watches).
- If the token request fails, the key is missing, mistyped or expired: delete
  it on the Garmin page, create a new one and update `.env.local`.
