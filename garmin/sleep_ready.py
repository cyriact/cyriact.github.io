#!/usr/bin/env python3
"""Exit 0 if last night's sleep has synced and Garmin has finalised it.

Usage:
  TZ=Asia/Kolkata python3 sleep_ready.py

Exit codes: 0 ready, 1 not yet (watch not synced or night still open),
2 can't check (key missing or the API refused). One API call, no data saved
except the cached token.
"""

import sys
from datetime import date

from pull import DATA, api_get, get_token, load_key


def main():
    today = date.today().isoformat()
    DATA.mkdir(exist_ok=True)
    try:
        token = get_token(load_key())
        nights = api_get(token, f"/sleep-service/sleep/dailySleepSummariesByDate?startDate={today}&endDate={today}") or []
    except SystemExit as e:
        print(e)
        sys.exit(2)
    except Exception as e:  # network or API failure
        print(f"Could not check sleep: {e}")
        sys.exit(2)
    night = next((n for n in nights if n.get("calendarDate") == today), None)
    if night and night.get("sleepTimeSeconds") and night.get("sleepWindowConfirmed"):
        print(f"Ready: {today}, {night['sleepTimeSeconds'] / 3600:.1f} h sleep")
        sys.exit(0)
    print(f"Not yet: no finalised sleep for {today}")
    sys.exit(1)


if __name__ == "__main__":
    main()
