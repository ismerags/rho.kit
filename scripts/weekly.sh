#!/bin/bash
# The weekly job: check every retailer, rebuild the data files, push.
#
# GitHub Actions then rebuilds and republishes the website, usually within two
# minutes of the push.
#
# Run it by hand any time:   ./scripts/weekly.sh
# Or let launchd run it — see scripts/install-schedule.sh
#
# Safety property worth knowing about: this only ever stages `data/`. If you
# were halfway through editing something else when the schedule fired, your
# work in progress is not swept into an automated commit.

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PROJECT_DIR="$(pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

# Set LEGO_AUTO_PUSH=0 to stop before pushing and review the diff yourself.
AUTO_PUSH="${LEGO_AUTO_PUSH:-1}"

echo "=============================================================="
echo "LEGO price collection — $(date '+%A %-d %B %Y, %H:%M')"
echo "=============================================================="

# ---------------------------------------------------------------- 1. collect
# `priceall` skips anything priced in the last 7 days, so if this is
# interrupted, the next run picks up where it stopped rather than starting over.
echo
echo "[1/3] Checking every retailer. This takes about 45 minutes."
if ! "$PYTHON" -m legotracker priceall; then
    echo
    echo "  Collection stopped early. Publishing whatever was gathered —"
    echo "  a partial update is better than a week of stale prices, and every"
    echo "  price on the site carries its own date anyway."
fi

# ---------------------------------------------------- 2. export and rebuild
echo
echo "[2/3] Writing the data files and rebuilding the site."
if ! "$PYTHON" -m legotracker publish; then
    echo "  Build failed. Nothing has been committed." >&2
    exit 1
fi

# ------------------------------------------------------------- 3. push
echo
if [ ! -d .git ]; then
    echo "[3/3] Not a git repository yet — skipping the push."
    echo "      See DEPLOY.md to create the repository."
    exit 0
fi

git add data/
if git diff --cached --quiet; then
    echo "[3/3] No price changes since the last run. Nothing to push."
    exit 0
fi

CHANGED=$(git diff --cached --numstat data/observations.csv | awk '{print $1}')
git commit -q -m "Prices for $(date '+%-d %B %Y')

${CHANGED:-0} new observations, collected automatically."
echo "[3/3] Committed ${CHANGED:-0} new observations."

if [ "$AUTO_PUSH" != "1" ]; then
    echo "      LEGO_AUTO_PUSH=0 — not pushing. Run 'git push' when ready."
    exit 0
fi

if ! git remote get-url origin >/dev/null 2>&1; then
    echo "      No 'origin' remote set, so nothing was pushed."
    exit 0
fi

if git push -q; then
    echo "      Pushed. The site rebuilds in about two minutes."
else
    echo "      Push failed — the commit is saved locally, so nothing is lost." >&2
    echo "      Run 'git push' yourself to see why." >&2
    exit 1
fi
