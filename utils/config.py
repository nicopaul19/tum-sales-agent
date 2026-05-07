"""
Configuration management for TUM Sales Agent.
Loads environment variables and provides paths.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Project Paths (define early for env loading)
PROJECT_ROOT = Path(__file__).parent.parent
WORKSPACE_ROOT = PROJECT_ROOT.parent  # AI Projects & Agents/

# Load environment variables (cascade: root → project)
load_dotenv(WORKSPACE_ROOT / ".env")  # Shared keys (fallback)
load_dotenv(PROJECT_ROOT / ".env", override=True)  # Project-specific (overrides)

# API Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DB_QUALIFIED_ID = os.getenv("NOTION_DB_QUALIFIED_ID")
NOTION_DB_ACCOUNTS_ID = os.getenv("NOTION_DB_ACCOUNTS_ID")  # Existing companies/accounts database
NOTION_DB_CONTACTS_ID = os.getenv("NOTION_DB_CONTACTS_ID")  # Contacts database

# Email Delivery (Gmail App Password)
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
REPORT_RECIPIENT_EMAIL = os.getenv("REPORT_RECIPIENT_EMAIL")
RANKING_REPORT_RECIPIENTS = os.getenv("RANKING_REPORT_RECIPIENTS")  # Comma-separated email list for ranking reports
FEEDBACK_REPORT_RECIPIENTS = os.getenv("FEEDBACK_REPORT_RECIPIENTS")  # Comma-separated email list for feedback reports
DEFAULT_CAMPAIGN_SENDER = os.getenv("DEFAULT_CAMPAIGN_SENDER")  # Optional default outreach sender full name

# Data Paths
DATA_DIR = PROJECT_ROOT / "data"
INPUTS_DIR = DATA_DIR / "inputs"
TABLES_DIR = DATA_DIR / "tables"
LOGS_DIR = DATA_DIR / "logs"

# Screenshot Input Folders
IMAGES_DIR = INPUTS_DIR / "images"
IMAGES_NEW_DIR = IMAGES_DIR / "new"
IMAGES_PROCESSED_DIR = IMAGES_DIR / "processed"

# LinkedIn URL Input Folders (for post URLs)
LINKEDIN_URLS_DIR = INPUTS_DIR / "linkedin_urls"
LINKEDIN_URLS_NEW_DIR = LINKEDIN_URLS_DIR / "new"
LINKEDIN_URLS_PROCESSED_DIR = LINKEDIN_URLS_DIR / "processed"

# Manual Contact Input Folders (for individual profile URLs with company name)
MANUAL_CONTACTS_DIR = INPUTS_DIR / "manual_contacts"
MANUAL_CONTACTS_NEW_DIR = MANUAL_CONTACTS_DIR / "new"
MANUAL_CONTACTS_PROCESSED_DIR = MANUAL_CONTACTS_DIR / "processed"

# LinkedIn Dump Folder (manually saved LinkedIn HTML pages)
LINKEDIN_DUMP_DIR = INPUTS_DIR / "linkedin_dump"

# CSV Files
MASTER_CSV = TABLES_DIR / "master_input.csv"
QUALIFIED_CSV = TABLES_DIR / "weekly_qualified_leads_with_contacts.csv"
QUALIFIED_NO_CONTACT_CSV = TABLES_DIR / "weekly_qualified_leads_no_contact.csv"
BACKLOG_CSV = TABLES_DIR / "backlog.csv"
EXPORTED_ARCHIVE_CSV = TABLES_DIR / "exported_archive.csv"
REQUALIFIED_BLOCKED_CSV = TABLES_DIR / "requalified_blocked_leads.csv"
NO_PERSON_CSV = TABLES_DIR / "no_person_found_at_lead_account.csv"
REPORTS_DIR = DATA_DIR / "reports"
API_USAGE_LOG = LOGS_DIR / "api_usage.jsonl"

# CSV Headers
MASTER_CSV_HEADERS = [
    "date_added",
    "company_name",
    "company_domain",
    "person_name",
    "linkedin_url_contact",
    "linkedin_url_post",
    "trigger",
    "score",
    "reasoning",
    "source",
    "status"
]

# Blocklist - companies to automatically filter out (case-insensitive)
COMPANY_BLOCKLIST = [
    "tum.ai",
    "tum ai",
    "tumai",
    "tum-ai",
    "helsing",        # Defense/weapons company - doesn't align with our values
    "rheinmetall",    # Defense/weapons manufacturer
    # Add more false positives here as discovered
]


def validate_config():
    """Check that required API keys are set."""
    missing = []
    if not OPENAI_API_KEY:
        missing.append("OPENAI_API_KEY")
    if not NOTION_TOKEN:
        missing.append("NOTION_TOKEN")
    if not NOTION_DB_QUALIFIED_ID:
        missing.append("NOTION_DB_QUALIFIED_ID")

    if missing:
        return False, missing
    return True, []
