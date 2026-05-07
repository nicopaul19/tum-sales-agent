#!/usr/bin/env python3
"""
Create the "Project Requirements" Notion database for TUM Social AI.

This database stores NGO form responses from Tally and links to the
existing Accounts and Contacts databases via relations + rollups.

Usage:
    cd tum_sales_agent
    source venv/bin/activate
    python scripts/create_requirements_db.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv
from pathlib import Path

# Load env (root first, then project-specific)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
ACCOUNTS_DB_ID = os.getenv("NOTION_DB_ACCOUNTS_ID")
CONTACTS_DB_ID = os.getenv("NOTION_DB_CONTACTS_ID")

# Social Partnerships Home page — the DB will live inside a child page here.
SOCIAL_PARTNERSHIPS_HOME_ID = "293a0c6e-6168-81ab-974c-e873a1c89f4b"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

API_BASE = "https://api.notion.com/v1"


def create_parent_page() -> str:
    """Create the parent page 'Project Requirements Form Submission NGOs'."""
    payload = {
        "parent": {"type": "page_id", "page_id": SOCIAL_PARTNERSHIPS_HOME_ID},
        "icon": {"type": "emoji", "emoji": "📋"},
        "properties": {
            "title": {"title": [{"text": {"content": "Project Requirements Form Submission NGOs"}}]}
        },
    }
    print("Creating parent page 'Project Requirements Form Submission NGOs'...")
    resp = requests.post(f"{API_BASE}/pages", headers=HEADERS, json=payload)
    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)
    page = resp.json()
    page_id = page["id"]
    print(f"  Page created: {page_id}")
    print(f"  URL: {page['url']}")
    return page_id


def add_link_to_home(page_id: str):
    """Add a link to the new page on the Social Partnerships Home."""
    # Add a heading + link_to_page block
    blocks = [
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "NGO Project Requirements Submissions"}}]
            },
        },
        {
            "object": "block",
            "type": "link_to_page",
            "link_to_page": {"type": "page_id", "page_id": page_id},
        },
    ]
    resp = requests.patch(
        f"{API_BASE}/blocks/{SOCIAL_PARTNERSHIPS_HOME_ID}/children",
        headers=HEADERS,
        json={"children": blocks},
    )
    if resp.status_code != 200:
        print(f"WARNING: Could not add link to Home: {resp.status_code}: {resp.text}")
    else:
        print("  Link added to Social Partnerships Home page")


def create_database(parent_page_id: str):
    """Create the Project Requirements database with all properties."""

    # ── Property definitions ──────────────────────────────────────────
    # Order matters visually in Notion (first = leftmost column after title).

    properties = {
        # ─── TITLE (Organization Name) ───
        "Organization Name": {"title": {}},

        # ─── RELATIONS ───
        "Account": {
            "relation": {
                "database_id": ACCOUNTS_DB_ID,
                "single_property": {},  # single relation (one account per form)
            }
        },
        "Product Owner": {
            "relation": {
                "database_id": CONTACTS_DB_ID,
                "single_property": {},
            }
        },

        # ─── SECTION 1: Project Scope & Impact ───
        "Problem Statement": {
            "rich_text": {},
        },
        "Current Effort": {
            "rich_text": {},
        },
        "Usage Frequency": {
            "rich_text": {},
        },
        "Additional Benefits": {
            "rich_text": {},
        },

        # ─── SECTION 2: Data Readiness ───
        "Data Availability": {
            "select": {
                "options": [
                    {"name": "1-7 Days (Ready)", "color": "green"},
                    {"name": "Longer / Delayed", "color": "red"},
                ]
            }
        },
        "Data Delay Details": {
            "rich_text": {},
        },
        "Data Language": {
            "rich_text": {},
        },

        # ─── SECTION 3: Technical Logistics ───
        "AWS Credits": {
            "select": {
                "options": [
                    {"name": "Yes - will open/have account", "color": "green"},
                    {"name": "No - will self-fund", "color": "yellow"},
                    {"name": "No budget for infrastructure", "color": "red"},
                ]
            }
        },
        "Post-Deployment Sustainability": {
            "select": {
                "options": [
                    {"name": "Yes - can cover recurring costs", "color": "green"},
                    {"name": "No budget for recurring costs", "color": "red"},
                ]
            }
        },
        "Tech Ecosystem": {
            "multi_select": {
                "options": [
                    {"name": "Microsoft 365 / Teams", "color": "blue"},
                    {"name": "Google Workspace / Drive", "color": "green"},
                    {"name": "Slack / Discord", "color": "purple"},
                    {"name": "Custom Internal Software", "color": "gray"},
                ]
            }
        },

        # ─── SECTION 4: Commitment & Timeline ───
        # PO fields filled by Tally (also used for Contact enrichment)
        "PO Name": {
            "rich_text": {},
        },
        "PO Role": {
            "rich_text": {},
        },
        "PO Email": {
            "email": {},
        },
        "PO Phone": {
            "phone_number": {},
        },
        "PO English Fluency": {
            "select": {
                "options": [
                    {"name": "Confirmed (Professional+)", "color": "green"},
                    {"name": "No (Cannot communicate in English)", "color": "red"},
                ]
            }
        },
        "PO Technical Competence": {
            "select": {
                "options": [
                    {"name": "1 - Non-Technical", "color": "gray"},
                    {"name": "2 - Basic Digital Literacy", "color": "blue"},
                    {"name": "3 - Tech-Savvy", "color": "yellow"},
                    {"name": "4 - Technical", "color": "orange"},
                    {"name": "5 - Expert", "color": "green"},
                ]
            }
        },
        "Weekly Check-in": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "No", "color": "red"},
                ]
            }
        },
        "Cohort": {
            "select": {
                "options": [
                    {"name": "Summer Semester 2026", "color": "yellow"},
                    {"name": "Winter Semester 2026/2027", "color": "blue"},
                ]
            }
        },
        "Kick-Off & Demo Day Attendance": {
            "select": {
                "options": [
                    {"name": "Yes - both", "color": "green"},
                    {"name": "No - cannot attend", "color": "red"},
                ]
            }
        },
        "Format Preference": {
            "select": {
                "options": [
                    {"name": "Semester Project", "color": "blue"},
                    {"name": "Hackathon", "color": "orange"},
                    {"name": "Thesis Topic", "color": "purple"},
                    {"name": "Either", "color": "gray"},
                ]
            }
        },

        # ─── SECTION 5: Marketing & Sign-off ───
        "Marketing Permission": {
            "select": {
                "options": [
                    {"name": "Yes", "color": "green"},
                    {"name": "No (Confidential)", "color": "red"},
                ]
            }
        },
        "Signatory Name": {
            "rich_text": {},
        },
        "Signature Date": {
            "date": {},
        },
        "Signature (Typed)": {
            "rich_text": {},
        },

        # ─── META ───
        "Submission Date": {
            "created_time": {},
        },
        "Status": {
            "select": {
                "options": [
                    {"name": "New", "color": "blue"},
                    {"name": "Under Review", "color": "yellow"},
                    {"name": "Accepted", "color": "green"},
                    {"name": "Rejected", "color": "red"},
                    {"name": "Needs Info", "color": "orange"},
                ]
            }
        },
    }

    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "📋"},
        "title": [
            {
                "type": "text",
                "text": {"content": "Project Requirements"},
            }
        ],
        "properties": properties,
    }

    print("Creating 'Project Requirements' database in Notion...")
    resp = requests.post(f"{API_BASE}/databases", headers=HEADERS, json=payload)

    if resp.status_code != 200:
        print(f"ERROR {resp.status_code}: {resp.text}")
        sys.exit(1)

    db = resp.json()
    db_id = db["id"]
    db_url = db["url"]

    print(f"\nDatabase created!")
    print(f"  ID:  {db_id}")
    print(f"  URL: {db_url}")

    return db_id


def add_rollups(db_id: str):
    """
    Add rollup properties that pull data from the related Account and Contact.
    Rollups can only be added AFTER the database (and its relation properties) exist.
    """

    rollups = {
        # ─── From Account ───
        "Account Status": {
            "rollup": {
                "relation_property_name": "Account",
                "rollup_property_name": "Status",
                "function": "show_original",
            }
        },
        "Account Website": {
            "rollup": {
                "relation_property_name": "Account",
                "rollup_property_name": "Website URL*",
                "function": "show_original",
            }
        },
        "Account Type": {
            "rollup": {
                "relation_property_name": "Account",
                "rollup_property_name": "Account Type*",
                "function": "show_original",
            }
        },
        # ─── From Product Owner (Contact) ───
        "PO LinkedIn": {
            "rollup": {
                "relation_property_name": "Product Owner",
                "rollup_property_name": "LinkedIn",
                "function": "show_original",
            }
        },
        "PO Contact Email": {
            "rollup": {
                "relation_property_name": "Product Owner",
                "rollup_property_name": "Email",
                "function": "show_original",
            }
        },
    }

    print("\nAdding rollup properties...")
    resp = requests.patch(
        f"{API_BASE}/databases/{db_id}",
        headers=HEADERS,
        json={"properties": rollups},
    )

    if resp.status_code != 200:
        print(f"ERROR adding rollups: {resp.status_code}: {resp.text}")
        # Print details for debugging
        try:
            err = resp.json()
            print(json.dumps(err, indent=2))
        except Exception:
            pass
        return

    print("Rollups added successfully!")
    print("  - Account Status (from Accounts DB)")
    print("  - Account Website (from Accounts DB)")
    print("  - Account Type (from Accounts DB)")
    print("  - PO LinkedIn (from Contacts DB)")
    print("  - PO Contact Email (from Contacts DB)")


def main():
    if not all([NOTION_TOKEN, ACCOUNTS_DB_ID, CONTACTS_DB_ID]):
        print("Missing required environment variables:")
        print(f"  NOTION_TOKEN:        {'set' if NOTION_TOKEN else 'MISSING'}")
        print(f"  NOTION_DB_ACCOUNTS_ID: {'set' if ACCOUNTS_DB_ID else 'MISSING'}")
        print(f"  NOTION_DB_CONTACTS_ID: {'set' if CONTACTS_DB_ID else 'MISSING'}")
        sys.exit(1)

    print("=" * 60)
    print("TUM Social AI — Project Requirements DB Creator")
    print("=" * 60)
    print(f"\nAccounts DB: {ACCOUNTS_DB_ID}")
    print(f"Contacts DB: {CONTACTS_DB_ID}")
    print()

    parent_page_id = create_parent_page()
    add_link_to_home(parent_page_id)
    db_id = create_database(parent_page_id)
    add_rollups(db_id)

    print("\n" + "=" * 60)
    print("DONE!")
    print("=" * 60)
    print(f"\nNew DB ID: {db_id}")
    print("\nNext steps:")
    print("  1. Add this to your .env: NOTION_DB_REQUIREMENTS_ID=" + db_id)
    print("  2. Create the Tally form (see tally_form_structure.md)")
    print("  3. Connect Tally -> Notion integration to this database")
    print("  4. Run the enrichment script after each form submission")


if __name__ == "__main__":
    main()
