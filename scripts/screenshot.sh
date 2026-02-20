#!/bin/bash
# Capture a screenshot and save it directly to the collector's image input folder.
# Usage:
#   ./scripts/screenshot.sh           # interactive selection (crosshair)
#   ./scripts/screenshot.sh --window  # capture a specific window
#
# Tip: Bind to a global keyboard shortcut via Automator or Raycast.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMG_DIR="$PROJECT_DIR/data/inputs/images/new"

FILENAME="lead_$(date +%Y%m%d_%H%M%S).png"
FILEPATH="$IMG_DIR/$FILENAME"

if [ "$1" = "--window" ]; then
    screencapture -w "$FILEPATH"
else
    screencapture -i "$FILEPATH"
fi

if [ -f "$FILEPATH" ]; then
    echo "Screenshot saved: $FILENAME"
else
    echo "Screenshot cancelled."
fi
