# Notes for Claude

## Garmin data

`garmin/connector/` is a Cloudflare Worker MCP connector (tools `garmin_today`, `garmin_sleep`,
`garmin_hrv`, `garmin_daily`, `garmin_activities`, ...). When the Garmin connector's tools are loaded
in a session, use them for quick questions. If they aren't loaded or fail to connect, or you need
bulk history, use the scripts in `garmin/`. Both call Garmin's official, read-only Personal API with
the `GARMIN_PERSONAL_API_KEY` environment variable (set in the cloud environment; locally it can live
in `garmin/.env.local`; on the Worker it is a secret). Never print the key.

- `cd garmin && TZ=Asia/Kolkata python3 pull.py` saves (or tops up) history in `garmin/data/`:
  `activities.json` (list), and `daily.json`, `sleep.json`, `hrv.json`, `weight.json` (keyed by date),
  plus `fitness_age.json` and `hr_zones.json`. A first run takes about a minute; re-runs only fetch what's new.
- Read those JSON files to answer questions (recent runs, sleep, HRV, resting HR, Body Battery).
  Distances are meters, durations seconds, weights grams. The user is in India time (Asia/Kolkata).
- `garmin/data/` and `garmin/.env.local` are gitignored. This repo is a public GitHub Pages site:
  never commit health data or the key.
- The hosted "Training & Recovery" dashboard is refreshed every morning by a scheduled task
  (`sleep_ready.py`, `pull.py`, `export_dash.py`); see `garmin/README.md`.
