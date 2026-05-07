#!/bin/bash
# Run the Notion cleanup agent with venv activation and logging.
# Used by launchd for scheduled runs (1st and 15th of each month at 10:00).
#
# Phase 1 (domains) runs automatically.
# Phase 2 (merges) requires interactive confirmation — opens Terminal.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/notion_cleanup_$(date +%Y%m%d).log"

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

echo "=== Notion Cleanup run: $(date) ===" >> "$LOG_FILE"

# Phase 1: Domain population (runs in background, output to log)
source "$PROJECT_DIR/venv/bin/activate"
cd "$PROJECT_DIR"
python3 -m agents.notion_cleanup --domains >> "$LOG_FILE" 2>&1

PHASE1_EXIT=$?
echo "=== Phase 1 exit code: $PHASE1_EXIT ===" >> "$LOG_FILE"

# Phase 2: Duplicate merge (needs interactive confirmation — open Terminal)
if [ $PHASE1_EXIT -eq 0 ]; then
    osascript -e "
        tell application \"Terminal\"
            activate
            do script \"cd '$PROJECT_DIR' && source venv/bin/activate && python3 -m agents.notion_cleanup --merge; echo ''; echo 'Press any key to close...'; read -n 1\"
        end tell
    "
    echo "=== Phase 2 launched in Terminal for interactive review ===" >> "$LOG_FILE"
else
    echo "=== Phase 1 failed, skipping Phase 2 ===" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
exit $PHASE1_EXIT
