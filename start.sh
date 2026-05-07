#!/bin/bash

# Start script for Sports Betting Bot
# This script starts bot.py in the background and saves its PID

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"

# Check if bot is already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "Bot is already running with PID $OLD_PID"
        exit 1
    fi
fi

# Start the bot in the background
cd "$SCRIPT_DIR"
nohup python3 bot.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!

# Save the PID to file
echo $NEW_PID > "$PID_FILE"

echo "Bot started with PID $NEW_PID"
echo "Logs are being written to: $LOG_FILE"
