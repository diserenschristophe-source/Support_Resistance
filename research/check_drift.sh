#!/bin/bash
set -euo pipefail
#
# check_drift.sh — Compare server files against GitHub repo
#
# Setup (one-time):
#   1. Create a GitHub personal access token (read-only, repo scope)
#   2. Set it:  export GITHUB_TOKEN="ghp_..."
#   3. Update REPO below with your GitHub user/repo
#
# Usage:
#   ./check_drift.sh              # check all tracked files
#   ./check_drift.sh --notify     # also send alert (configure below)
#
# Cron example (check daily at 04:00 UTC):
#   0 4 * * * /opt/sr-dashboard/check_drift.sh --notify >> /opt/sr-dashboard/output/drift.log 2>&1
#

REPO="xris/sr-dashboard"          # ← update with your GitHub username/repo
BRANCH="main"
SERVER_DIR="/opt/sr-dashboard"

# Files to track — add/remove as needed
FILES=(
    "main.py"
    "dashboard.py"
    "chart.py"
    "report.py"
    "fetch_news.py"
    "daily_pipeline.sh"
    "core/config.py"
    "core/sr_analysis.py"
    "core/tpsl.py"
    "core/filters.py"
    "core/fetcher.py"
)

NOTIFY=""
if [[ "${1:-}" == "--notify" ]]; then
    NOTIFY="1"
fi

# ── GitHub API ──────────────────────────────────────────────
if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "ERROR: GITHUB_TOKEN not set. Export a personal access token with repo scope."
    exit 1
fi

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
echo "=================================================="
echo "  DRIFT CHECK — ${TIMESTAMP}"
echo "  Server: ${SERVER_DIR}"
echo "  Repo:   ${REPO} (${BRANCH})"
echo "=================================================="
echo ""

DRIFTED=()
MISSING_SERVER=()
MISSING_REPO=()
OK=()

for FILE in "${FILES[@]}"; do
    SERVER_FILE="${SERVER_DIR}/${FILE}"

    # Check server file exists
    if [[ ! -f "${SERVER_FILE}" ]]; then
        MISSING_SERVER+=("${FILE}")
        echo "  ✗ ${FILE} — MISSING on server"
        continue
    fi

    # Fetch file content from GitHub API (base64 encoded)
    RESPONSE=$(curl -s -w "\n%{http_code}" \
        -H "Authorization: token ${GITHUB_TOKEN}" \
        -H "Accept: application/vnd.github.v3.raw" \
        "https://api.github.com/repos/${REPO}/contents/${FILE}?ref=${BRANCH}" \
        2>/dev/null)

    HTTP_CODE=$(echo "${RESPONSE}" | tail -1)
    CONTENT=$(echo "${RESPONSE}" | sed '$d')

    if [[ "${HTTP_CODE}" == "404" ]]; then
        MISSING_REPO+=("${FILE}")
        echo "  ? ${FILE} — not found in repo"
        continue
    elif [[ "${HTTP_CODE}" != "200" ]]; then
        echo "  ! ${FILE} — GitHub API error (HTTP ${HTTP_CODE})"
        continue
    fi

    # Compare: hash the GitHub content vs server file
    GITHUB_HASH=$(echo "${CONTENT}" | shasum -a 256 | cut -d' ' -f1)
    SERVER_HASH=$(shasum -a 256 < "${SERVER_FILE}" | cut -d' ' -f1)

    if [[ "${GITHUB_HASH}" != "${SERVER_HASH}" ]]; then
        DRIFTED+=("${FILE}")
        # Show first few lines of diff
        DIFF=$(diff <(echo "${CONTENT}") "${SERVER_FILE}" 2>/dev/null | head -20 || true)
        echo "  ✗ ${FILE} — DRIFTED"
        echo "${DIFF}" | sed 's/^/      /'
        echo ""
    else
        OK+=("${FILE}")
        echo "  ✓ ${FILE}"
    fi
done

# ── Summary ─────────────────────────────────────────────────
echo ""
echo "──────────────────────────────────────────────────"
echo "  SUMMARY"
echo "  OK:             ${#OK[@]}"
echo "  Drifted:        ${#DRIFTED[@]}"
echo "  Missing server: ${#MISSING_SERVER[@]}"
echo "  Missing repo:   ${#MISSING_REPO[@]}"
echo "──────────────────────────────────────────────────"

if [[ ${#DRIFTED[@]} -gt 0 ]]; then
    echo ""
    echo "  FILES WITH DRIFT:"
    for F in "${DRIFTED[@]}"; do
        echo "    - ${F}"
    done
fi

if [[ ${#MISSING_SERVER[@]} -gt 0 ]]; then
    echo ""
    echo "  MISSING ON SERVER:"
    for F in "${MISSING_SERVER[@]}"; do
        echo "    - ${F}"
    done
fi

# ── Notification (optional) ─────────────────────────────────
if [[ -n "${NOTIFY}" && ( ${#DRIFTED[@]} -gt 0 || ${#MISSING_SERVER[@]} -gt 0 ) ]]; then
    MSG="⚠️ SR-Dashboard drift detected at ${TIMESTAMP}\n"
    MSG+="Drifted: ${DRIFTED[*]:-none}\n"
    MSG+="Missing: ${MISSING_SERVER[*]:-none}\n"
    MSG+="Run: ssh server 'cd ${SERVER_DIR} && git pull'"

    # Uncomment ONE of these notification methods:

    # Option A: Slack webhook
    # SLACK_WEBHOOK="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
    # curl -s -X POST -H 'Content-type: application/json' \
    #     --data "{\"text\":\"${MSG}\"}" "${SLACK_WEBHOOK}"

    # Option B: Telegram bot
    # TG_TOKEN="your_bot_token"
    # TG_CHAT="your_chat_id"
    # curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    #     -d "chat_id=${TG_CHAT}" -d "text=${MSG}" -d "parse_mode=HTML"

    echo ""
    echo "  Notification: sent (configure Slack/Telegram in script)"
fi

echo ""
exit ${#DRIFTED[@]}
