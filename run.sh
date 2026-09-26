#!/bin/bash
# Cron wrapper for the Micro1 scraper — same shape as turing-scraper/run.sh.
# Suggested crontab entry (every 4 hours, same cadence as Mercor/Turing):
#   0 */4 * * * bash /Users/levongevorgyan/Documents/micro1-scraper/run.sh
# Uses the system python3 (stdlib only, no venv needed).

cd "$(dirname "$0")"
/usr/bin/python3 scraper.py >> scraper.log 2>&1
