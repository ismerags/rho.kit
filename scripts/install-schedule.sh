#!/bin/bash
# Set up the weekly collection on this Mac. Run it once.
#
#   ./scripts/install-schedule.sh
#
# It fills the real project path into the launchd template and loads it.
# To stop:  ./scripts/install-schedule.sh --remove

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
LABEL="com.legotracker.weekly"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ "${1:-}" = "--remove" ]; then
    launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Weekly collection removed. Nothing else changed."
    exit 0
fi

if [ "$(uname)" != "Darwin" ]; then
    echo "This installs a macOS launchd job. On Linux, use cron:" >&2
    echo "  0 3 * * 0 $PROJECT_DIR/scripts/weekly.sh" >&2
    exit 1
fi

chmod +x scripts/weekly.sh

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    scripts/com.legotracker.weekly.plist > "$TARGET"

launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Installed. Collection runs every Sunday at 03:00."
echo
echo "  Check it:    launchctl list | grep legotracker"
echo "  Run it now:  launchctl start $LABEL"
echo "  See the log: tail -f $PROJECT_DIR/data/weekly.log"
echo "  Remove it:   ./scripts/install-schedule.sh --remove"
echo
echo "Note: launchd cannot wake a sleeping Mac. If yours sleeps on Sunday"
echo "nights, the job runs as soon as it next wakes instead — the site will"
echo "say how old the prices are either way."
