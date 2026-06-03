#!/bin/bash
# Quick-add a manual contact to the collector queue.
# Usage:
#   ./scripts/add_contact.sh "https://linkedin.com/in/person" "Company Name" "Met at event"
#   ./scripts/add_contact.sh "https://linkedin.com/in/person" "Company Name"
#
# If no trigger is given, defaults to "Manual contact upload".

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONTACT_FILE="$PROJECT_DIR/data/inputs/manual_contacts/new/contacts.txt"

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: add_contact.sh <linkedin_url> <company_name> [trigger]"
    exit 1
fi

LINKEDIN_URL="$1"
COMPANY_NAME="$2"
TRIGGER="${3:-Manual contact upload}"

mkdir -p "$(dirname "$CONTACT_FILE")"
echo "$LINKEDIN_URL,$COMPANY_NAME,$TRIGGER" >> "$CONTACT_FILE"
echo "Added: $COMPANY_NAME ($LINKEDIN_URL)"
