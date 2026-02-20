#!/bin/bash
# Save the current LinkedIn connections/network page as complete HTML via Cmd+S,
# then run the LinkedIn analysis pipeline automatically.
#
# Supported LinkedIn pages:
#   - linkedin.com/mynetwork   → saves as network_YYYYMMDD_HHMMSS.html
#   - linkedin.com/search      → saves as network_YYYYMMDD_HHMMSS.html
#
# Uses automated Cmd+S (Save As) to capture fully rendered page content.
#
# Supported browsers: Google Chrome, Safari
#
# Usage:
#   ./scripts/save_linkedin.sh
#
# Tip: Bind to a global keyboard shortcut via Automator or Raycast.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
DUMP_DIR="$PROJECT_DIR/data/inputs/linkedin_dump"

# Ensure dump directory exists
mkdir -p "$DUMP_DIR"

# Detect frontmost browser and get URL
FRONT_APP=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true')

case "$FRONT_APP" in
    "Google Chrome")
        PAGE_URL=$(osascript -e 'tell application "Google Chrome" to get URL of active tab of front window')
        ;;
    "Safari")
        PAGE_URL=$(osascript -e 'tell application "Safari" to get URL of current tab of front window')
        ;;
    *)
        osascript -e "display notification \"Open a LinkedIn page in Chrome or Safari first\" with title \"Save LinkedIn\" sound name \"Basso\""
        echo "Error: Frontmost app is '$FRONT_APP' — expected Google Chrome or Safari"
        exit 1
        ;;
esac

# Only accept network/connections pages
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [[ "$PAGE_URL" == *"linkedin.com/mynetwork"* ]] || [[ "$PAGE_URL" == *"linkedin.com/search"* ]]; then
    FILENAME="network_${TIMESTAMP}.html"
else
    osascript -e "display notification \"Navigate to LinkedIn My Network first\" with title \"Save LinkedIn\" sound name \"Basso\""
    echo "Error: Not a supported LinkedIn page: $PAGE_URL"
    echo "Supported: linkedin.com/mynetwork, linkedin.com/search"
    exit 1
fi

FILEPATH="$DUMP_DIR/$FILENAME"

# Automate Cmd+S → set filename → set directory → save
osascript <<APPLESCRIPT
tell application "$FRONT_APP" to activate
delay 0.3

tell application "System Events"
    -- Trigger Save As (Cmd+S)
    keystroke "s" using command down
    delay 1.5

    -- Wait for save dialog to appear
    repeat 10 times
        if exists sheet 1 of window 1 of process "$FRONT_APP" then
            exit repeat
        end if
        delay 0.3
    end repeat

    tell process "$FRONT_APP"
        -- Set filename
        set value of text field 1 of sheet 1 of window 1 to "$FILENAME"
        delay 0.3

        -- Navigate to save directory via Cmd+Shift+G (Go to Folder)
        keystroke "g" using {command down, shift down}
        delay 1

        -- Type the path and press Enter
        keystroke "$DUMP_DIR"
        delay 0.5
        keystroke return
        delay 1

        -- Click Save
        click button "Save" of sheet 1 of window 1
    end tell
end tell
APPLESCRIPT

# Wait for file to be written (Chrome can take a while for large pages)
WAITED=0
MAX_WAIT=15
while [ $WAITED -lt $MAX_WAIT ]; do
    if [ -f "$FILEPATH" ] && [ -s "$FILEPATH" ]; then
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

# Check for the HTML file
if ! [ -f "$FILEPATH" ] || ! [ -s "$FILEPATH" ]; then
    osascript -e "display notification \"Save may have failed — check $DUMP_DIR\" with title \"Save LinkedIn\" sound name \"Basso\""
    echo "Warning: Expected file not found at $FILEPATH after ${MAX_WAIT}s"
    echo "Check $DUMP_DIR for the saved file."
    exit 1
fi

# Clean up the companion _files directory if created (we only need the HTML)
FILES_DIR="${FILEPATH%.html}_files"
if [ -d "$FILES_DIR" ]; then
    rm -rf "$FILES_DIR"
fi

echo "Saved: $FILEPATH"

# Run analysis immediately
osascript -e "display notification \"Network saved — running analysis...\" with title \"LinkedIn Agent\" sound name \"Glass\""

cd "$PROJECT_DIR"
source "$PROJECT_DIR/venv/bin/activate"
python -m agents.linkedin_manager 2>&1
ANALYSIS_EXIT=$?

if [ $ANALYSIS_EXIT -eq 0 ]; then
    osascript -e "display notification \"LinkedIn analysis complete — check your email\" with title \"LinkedIn Agent\" sound name \"Glass\""
else
    osascript -e "display notification \"Analysis failed (exit $ANALYSIS_EXIT)\" with title \"LinkedIn Agent\" sound name \"Basso\""
fi
