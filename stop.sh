#!/bin/bash

# Stop script for Sports Betting Bot
# This script stops the bot.py process

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"

# Check if PID file exists
if [ ! -f "$PID_FILE" ]; then
    echo "Bot is not running (no PID file found)"
    exit 1
fi

# Read the PID
PID=$(cat "$PID_FILE")

# Check if process is running
if ! ps -p "$PID" > /dev/null 2>&1; then
    echo "Bot is not running (process $PID not found)"
    rm -f "$PID_FILE"
    exit 1
fi

# Kill the process
kill "$PID"

# Wait a moment for graceful shutdown, then force kill if needed
sleep 2
if ps -p "$PID" > /dev/null 2>&1; then
    kill -9 "$PID"
    echo "Bot force killed (PID: $PID)"
else
    echo "Bot stopped gracefully (PID: $PID)"
fi

# Remove the PID file
rm -f "$PID_FILE"
