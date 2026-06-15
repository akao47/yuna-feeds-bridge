#!/usr/bin/env bash
# GB10 cron wrapper for the ai-search.io bridge.
#
# D-A1: this runs on the GB10 box, NOT GitHub Actions — datacenter IPs get
# bot-walled by Vercel harder than the box's residential IP. It scrapes, then
# commits + pushes any changed feed/sidecar/heartbeat. Align ~17 min ahead of
# Yuna's 4h RSS poll, e.g. crontab:
#   17 */4 * * * /home/alexkao/yuna-feeds-bridge/run-aisearch.sh >> ~/aisearch-bridge.log 2>&1
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR" || exit 1

# AISEARCH_PYTHON should point at a venv python that has playwright installed
# (see README "Deploy on GB10"). Defaults to python3 on PATH.
PYTHON="${AISEARCH_PYTHON:-python3}"

# Stay current so the push fast-forwards instead of racing a stale local main.
git pull --ff-only --quiet || echo "warn: git pull --ff-only failed; continuing"

"$PYTHON" scrape_aisearch.py
SCRAPE_RC=$?

# Commit whatever changed — including an error heartbeat on a failed run, so
# downstream staleness is visible rather than looking healthy (Codex #10).
git add aisearch.xml aisearch.json aisearch-heartbeat.json 2>/dev/null || true
if ! git diff --cached --quiet; then
  git -c user.name='yuna-feeds-bridge[bot]' -c user.email='noreply@github.com' \
    commit -q -m "scrape: ai-search.io refresh ($(date -u +%Y-%m-%dT%H:%MZ), rc=$SCRAPE_RC)"
  git push -q || echo "warn: git push failed"
else
  echo "no change; nothing to commit"
fi

exit "$SCRAPE_RC"
