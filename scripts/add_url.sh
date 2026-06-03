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

URL="$(printf '%s' "$URL" | head -1 | tr -d '[:space:]')"

# Basic validation
if [[ ! "$URL" == *"linkedin.com"* ]]; then
    echo "Error: Not a LinkedIn URL: $URL"
    exit 1
fi

if [[ "$URL" == *"…"* || "$URL" == *"[...]"* || "$URL" == *"[…]"* ]]; then
    echo "Error: Clipboard contains a visually truncated URL. Copy the real LinkedIn post link instead."
    exit 1
fi

mkdir -p "$(dirname "$URL_FILE")"
touch "$URL_FILE"

if grep -Fxq "$URL" "$URL_FILE"; then
    echo "Already queued: $URL"
    exit 0
fi

echo "$URL" >> "$URL_FILE"
echo "Added: $URL"
