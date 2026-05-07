#!/bin/bash
# Run the Tally Requirements Enrichment script
# This script is triggered by launchd twice per week (Mon/Thu at 10:00 AM)
# It processes new Tally form submissions in the Notion Requirements DB

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_DIR/venv"
LOG_DIR="$PROJECT_DIR/data/logs"
LOG_FILE="$LOG_DIR/enrich_requirements_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "$LOG_DIR"

echo "=== Tally Requirements Enrichment ===" | tee "$LOG_FILE"
echo "Started: $(date)" | tee -a "$LOG_FILE"

# Activate virtualenv and run
source "$VENV_DIR/bin/activate"
# Run basic enrichment (accounts, contacts, relations)
python "$PROJECT_DIR/scripts/enrich_requirements.py" 2>&1 | tee -a "$LOG_FILE"
ENRICH_EXIT=${PIPESTATUS[0]}

if [ $ENRICH_EXIT -eq 0 ]; then
    echo "Running AI GTM Expert Analyzer..." | tee -a "$LOG_FILE"
    python "$PROJECT_DIR/scripts/requirements_analyzer.py" 2>&1 | tee -a "$LOG_FILE"
    ANALYZE_EXIT=${PIPESTATUS[0]}
    EXIT_CODE=$((ENRICH_EXIT + ANALYZE_EXIT))
else
    EXIT_CODE=$ENRICH_EXIT
fi

echo "Finished: $(date) (exit code: $EXIT_CODE)" | tee -a "$LOG_FILE"

# Clean up old logs (keep last 30)
ls -t "$LOG_DIR"/enrich_requirements_*.log 2>/dev/null | tail -n +31 | xargs rm -f 2>/dev/null || true

exit $EXIT_CODE
