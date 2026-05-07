#!/usr/bin/env python3
import time
from datetime import datetime
from market_data import export_league_csv

LEAGUES = ["mlb", "nba", "epl", "laliga", "bundesliga", "ligue1", "serie_a", "champions_league", "mls", "nfl", "nhl"]
HOURS = 240
INTERVAL = 30

def update_data():
    for league in LEAGUES:
        try:
            filename, rows = export_league_csv(
                league,
                hours=HOURS,
                live_grace_hours=8,
                debug=True,
                log=print,
                tz_name="Europe/Berlin",
            )
            print(f"{datetime.now().strftime('%H:%M:%S')} | {league} | {filename} | {len(rows)} rows")
        except Exception as e:
            print(f"{datetime.now().strftime('%H:%M:%S')} | {league} | ERROR | {e}")