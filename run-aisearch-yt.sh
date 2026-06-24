#!/usr/bin/env bash
# GB10 cron wrapper for the theAIsearch YouTube bridge (Yuna rss_aisearch_yt source).
#
# Runs on the GB10 box (residential IP + the local engine-router for the Ultra
# routing call), scrapes the channel, then commits + pushes any changed
# feed/sidecar/heartbeat. Align ~17 min ahead of Yuna's 4h RSS poll, e.g. crontab
# (offset from run-aisearch.sh so the two bridges don't push at the same minute):
#   23 */4 * * * /home/alexkao/yuna-feeds-bridge/run-aisearch-yt.sh >> ~/aisearch-yt-bridge.log 2>&1
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR" || exit 1

# AISEARCH_YT_PYTHON should point at a venv python that has the requirements
# (yt-dlp + faster-whisper + openai) installed. Defaults to python3 on PATH.
PYTHON="${AISEARCH_YT_PYTHON:-python3}"

# Stay current so the push fast-forwards instead of racing a stale local main.
git pull --ff-only --quiet || echo "warn: git pull --ff-only failed; continuing"

"$PYTHON" scrape_aisearch_yt.py
SCRAPE_RC=$?

# Commit whatever changed — including an error heartbeat on a failed run, so
# downstream staleness is visible rather than looking healthy (Codex #10).
git add aisearch_yt.xml aisearch_yt.json aisearch_yt-heartbeat.json 2>/dev/null || true
if ! git diff --cached --quiet; then
  git -c user.name='yuna-feeds-bridge[bot]' -c user.email='noreply@github.com' \
    commit -q -m "scrape: theAIsearch YouTube refresh ($(date -u +%Y-%m-%dT%H:%MZ), rc=$SCRAPE_RC)"
  git push -q || echo "warn: git push failed"
else
  echo "no change; nothing to commit"
fi

exit "$SCRAPE_RC"
