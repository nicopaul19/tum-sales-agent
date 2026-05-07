#!/bin/bash
# Run the ranking agent with venv activation and logging.
# Used by launchd for scheduled ranking runs (Tue/Thu at 10:00).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/ranking_$(date +%Y%m%d).log"

# Wait for Google Drive to be available (max 60 seconds)
TIMEOUT=60
ELAPSED=0
while [ ! -d "$PROJECT_DIR" ] && [ $ELAPSED -lt $TIMEOUT ]; do
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

if [ ! -d "$PROJECT_DIR" ]; then
    echo "ERROR: Project directory not available after ${TIMEOUT}s" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

echo "=== Ranking run: $(date) ===" >> "$LOG_FILE"

source "$PROJECT_DIR/venv/bin/activate"
cd "$PROJECT_DIR"
python3 -m agents.ranking_agent >> "$LOG_FILE" 2>&1

EXITCODE=$?
echo "=== Exit code: $EXITCODE ===" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

exit $EXITCODE
