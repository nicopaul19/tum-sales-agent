#!/bin/bash
# Quick-add a LinkedIn URL to the collector queue.
# Usage:
#   ./scripts/add_url.sh                  # pastes from clipboard
#   ./scripts/add_url.sh "https://..."    # uses argument
#
# Tip: Bind this to a global keyboard shortcut via Automator or Raycast.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
URL_FILE="$PROJECT_DIR/data/inputs/linkedin_urls/new/urls.txt"

if [ -n "$1" ]; then
    URL="$1"
else
    URL="$(pbpaste)"
fi

# Basic validation
if [[ ! "$URL" == *"linkedin.com"* ]]; then
    echo "Error: Not a LinkedIn URL: $URL"
    exit 1
fi

echo "$URL" >> "$URL_FILE"
echo "Added: $URL"
