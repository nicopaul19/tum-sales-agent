"""
Notion Cleanup Agent — Domain population, account type enrichment, and duplicate merging.

Phases:
1. Domain Population: Find and verify missing Website URL* fields, classify Account Type*
2. Duplicate Merge: Detect domain-based duplicates, merge with interactive confirmation

Usage:
    python -m agents.notion_cleanup --domains   # Phase 1 only
    python -m agents.notion_cleanup --merge     # Phase 2 only
    python -m agents.notion_cleanup --all       # Both phases (default for schedule)
"""
import sys
import re
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Tuple

from openai import OpenAI
from pydantic import BaseModel, Field
from rich.console import Console
from rich.table import Table

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import resilient_http as http_requests

from utils.config import (
    OPENAI_API_KEY,
    NOTION_TOKEN,
    NOTION_DB_ACCOUNTS_ID,
    NOTION_DB_CONTACTS_ID,
    LOGS_DIR,
)
from utils.api_logger import log_api_usage
from agents.collector import (
    verify_domain_exists,
    verify_company_on_homepage,
    find_valid_domain,
)
from utils.notion_client import _notion_api_headers, create_contact_in_notion

console = Console()

MERGE_LOG = LOGS_DIR / "merge_log.jsonl"

# Rate limiting
NOTION_DELAY = 0.35  # seconds between Notion API calls
GPT_DELAY = 1.0  # seconds between GPT-4o calls
MAX_RETRIES = 3

# Status hierarchy for merge winner determination (index = rank)
STATUS_HIERARCHY = [
    "Prospect Qualified",           # 0
    "Connect. Request sent",        # 1
    "Contact details wrong",        # 2
    "Voicemail sent",               # 3
    "Nurture",                      # 4
    "Contacted LinkedIn 🌐",        # 5
    "Contacted Mail 📩",            # 6
    "Engaged",                      # 7
    "Awaiting Callback",            # 8
    "Discovery Call Booked",        # 9
    "Partnership in Discovery",     # 10
    "Partnership next Semesters",   # 11
    "Prospect Unqualified",         # 12
    "Mentorship confirmed",         # 13
    "Partnership started",          # 14
    "Partnership finished",         # 15
]

# Fields that are read-only (never write to these)
READ_ONLY_FIELDS = {
    "Domain*", "Contacts Email", "Contacts Phone",
    "Main Contact First Name", "Main Contact Last Name",
    "ID", "Created time", "Last edited",
}

# Suspect fields on the Account page
SUSPECT_FIELDS = [
    "[Suspect] Contact Name",
    "[Suspect] Contact Email",
    "[Suspect] Contact Phone",
    "[Suspect] Job Title",
]

# Multi-select fields (merge = union of values)
MULTI_SELECT_FIELDS = {"Country", "Campaign ID"}

# Text fields to append (newline-separated)
APPEND_TEXT_FIELDS = {"Trigger Event", "Trigger Event (Corporates)"}

# Relation fields (re-link from loser to winner)
RELATION_FIELDS = {"Contacts", "Previous Meetings"}

# Owner field — always keep winner's value
OWNER_FIELD = "Owner*"


# =============================================================================
# Pydantic models for GPT-4o structured output
# =============================================================================

class AccountEnrichment(BaseModel):
    """GPT-4o output for domain resolution + account type classification."""
    domain: str = Field(description="Best-guess company domain (e.g. 'nvidia.de', 'celonis.com')")
    domain_confidence: str = Field(description="'high', 'medium', or 'low'")
    domain_reasoning: str = Field(description="Why this domain was chosen")
    account_type: str = Field(description="'NGO', 'Corporate', 'Academic', or 'Student Club'")
    account_type_confidence: str = Field(description="'high', 'medium', or 'low'")


# =============================================================================
# Notion API helpers with rate limiting and retry
# =============================================================================

def _notion_request(method: str, url: str, json_body: dict = None) -> Optional[dict]:
    """Make a Notion API request with rate limiting and retry."""
    headers = _notion_api_headers()

    for attempt in range(MAX_RETRIES):
        time.sleep(NOTION_DELAY)
        try:
            if method == "GET":
                resp = http_requests.get(url, headers=headers, timeout=30)
            elif method == "POST":
                resp = http_requests.post(url, headers=headers, json=json_body or {}, timeout=30)
            elif method == "PATCH":
                resp = http_requests.patch(url, headers=headers, json=json_body or {}, timeout=30)
            else:
                return None

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code in (429, 502, 503):
                wait = (attempt + 1) * 2
                console.print(f"[yellow]Notion {resp.status_code}, retrying in {wait}s...[/yellow]")
                time.sleep(wait)
                continue

            console.print(f"[red]Notion API error {resp.status_code}: {resp.json().get('message', resp.text[:200])}[/red]")
            return None

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep((attempt + 1) * 2)
                continue
            console.print(f"[red]Notion request failed: {e}[/red]")
            return None

    return None


def fetch_all_accounts() -> List[dict]:
    """Fetch all pages from the Notion Accounts database with full properties."""
    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not configured[/red]")
        return []

    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"
    results = []
    start_cursor = None

    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = _notion_request("POST", url, body)
        if not data:
            break

        results.extend(data.get("results", []))
        if not data.get("has_more", False):
            break
        start_cursor = data.get("next_cursor")

    console.print(f"[cyan]Fetched {len(results)} accounts from Notion[/cyan]")
    return results


def extract_title(page: dict) -> str:
    """Extract the title (Organization*) from a Notion page."""
    for prop_data in page.get("properties", {}).values():
        if prop_data.get("type") == "title":
            title_arr = prop_data.get("title", [])
            if title_arr:
                return title_arr[0].get("plain_text", "")
    return ""


def extract_url_prop(page: dict, prop_name: str) -> str:
    """Extract a URL property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "url":
        return prop.get("url") or ""
    return ""


def extract_status(page: dict) -> str:
    """Extract the Status property value."""
    prop = page.get("properties", {}).get("Status", {})
    if prop.get("type") == "status" and prop.get("status"):
        return prop["status"].get("name", "")
    return ""


def extract_select(page: dict, prop_name: str) -> str:
    """Extract a select property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name", "")
    return ""


def extract_multi_select(page: dict, prop_name: str) -> List[str]:
    """Extract multi-select property values."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "multi_select":
        return [item.get("name", "") for item in prop.get("multi_select", [])]
    return []


def extract_rich_text(page: dict, prop_name: str) -> str:
    """Extract rich_text property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "rich_text":
        texts = prop.get("rich_text", [])
        if texts:
            return "".join(t.get("plain_text", "") for t in texts)
    return ""


def extract_relation_ids(page: dict, prop_name: str) -> List[str]:
    """Extract relation property page IDs."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "relation":
        return [r.get("id", "") for r in prop.get("relation", []) if r.get("id")]
    return []


def extract_number(page: dict, prop_name: str) -> Optional[float]:
    """Extract a number property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "number":
        return prop.get("number")
    return None


def extract_email_prop(page: dict, prop_name: str) -> str:
    """Extract an email property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "email":
        return prop.get("email") or ""
    return ""


def extract_phone_prop(page: dict, prop_name: str) -> str:
    """Extract a phone_number property value."""
    prop = page.get("properties", {}).get(prop_name, {})
    if prop.get("type") == "phone_number":
        return prop.get("phone_number") or ""
    return ""


def normalize_domain_from_url(raw_url: str) -> str:
    """Extract and normalize domain from a full URL."""
    if not raw_url:
        return ""
    match = re.search(r'(?:https?://)?(?:www\.)?([^/\s]+)', raw_url.lower())
    if match:
        return match.group(1).rstrip("/")
    return ""


def count_filled_fields(page: dict) -> int:
    """Count non-empty property fields on a page."""
    count = 0
    for prop_name, prop_data in page.get("properties", {}).items():
        ptype = prop_data.get("type")
        if ptype == "title":
            if prop_data.get("title"):
                count += 1
        elif ptype == "rich_text":
            if prop_data.get("rich_text"):
                count += 1
        elif ptype == "url":
            if prop_data.get("url"):
                count += 1
        elif ptype == "select":
            if prop_data.get("select"):
                count += 1
        elif ptype == "multi_select":
            if prop_data.get("multi_select"):
                count += 1
        elif ptype == "status":
            if prop_data.get("status"):
                count += 1
        elif ptype == "relation":
            if prop_data.get("relation"):
                count += 1
        elif ptype == "number":
            if prop_data.get("number") is not None:
                count += 1
        elif ptype == "email":
            if prop_data.get("email"):
                count += 1
        elif ptype == "phone_number":
            if prop_data.get("phone_number"):
                count += 1
    return count


def fetch_homepage_content(domain: str, max_bytes: int = 5000) -> str:
    """Fetch first N bytes of a domain's homepage HTML for classification."""
    if not domain:
        return ""
    try:
        url = f"https://{domain}"
        resp = http_requests.get(url, timeout=5, allow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 500:
            return ""
        try:
            content = resp.content[:max_bytes].decode("utf-8", errors="ignore")
        except Exception:
            content = resp.content[:max_bytes].decode("latin-1", errors="ignore")
        # Strip HTML tags for cleaner text
        text = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:3000]
    except Exception:
        return ""


# =============================================================================
# Phase 1: Domain Population + Account Type Enrichment
# =============================================================================

def resolve_domain_with_gpt(company_name: str, country: str = "", city: str = "") -> Optional[AccountEnrichment]:
    """Use GPT-4o to guess the best domain and account type for a company."""
    if not OPENAI_API_KEY:
        return None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    prompt = f"""You are resolving the official website domain for a company and classifying its type.

COMPANY: {company_name}
COUNTRY: {country or 'Unknown'}
CITY: {city or 'Unknown'}

DOMAIN RESOLUTION RULES:
1. Global companies operating in DACH → prefer the .de domain (e.g., Amazon → amazon.de, NVIDIA → nvidia.de, Google → google.de)
2. Native DACH companies → use their primary domain (e.g., Celonis → celonis.com, FlixBus → flixbus.de)
3. NGOs and nonprofits → use their actual domain (e.g., UNICEF → unicef.org)
4. Provide the most specific, correct domain you know.
5. Do NOT include "www." or "https://".

ACCOUNT TYPE CLASSIFICATION:
- "NGO" — non-profits, foundations, charities, social enterprises, humanitarian organizations
- "Corporate" — for-profit companies, startups, scale-ups, VCs, consulting firms
- "Academic" — universities, research institutions, think tanks
- "Student Club" — student organizations, student-run initiatives

Provide your best guess with confidence level."""

    try:
        time.sleep(GPT_DELAY)
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert at identifying company websites and classifying organizations."},
                {"role": "user", "content": prompt},
            ],
            response_format=AccountEnrichment,
            max_tokens=300,
        )
        result = response.choices[0].message.parsed
        log_api_usage("notion_cleanup", "domain_resolution", "gpt-4o", response.usage, {"company": company_name})
        return result
    except Exception as e:
        console.print(f"[red]GPT-4o error for {company_name}: {e}[/red]")
        return None


def classify_account_type_with_homepage(company_name: str, homepage_text: str) -> Optional[AccountEnrichment]:
    """Use GPT-4o to classify account type from homepage content."""
    if not OPENAI_API_KEY or not homepage_text:
        return None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    prompt = f"""Classify this organization based on its homepage content.

COMPANY: {company_name}
HOMEPAGE CONTENT (first ~3KB):
{homepage_text[:2500]}

ACCOUNT TYPE OPTIONS (pick exactly one):
- "NGO" — non-profits, foundations, charities, social enterprises, humanitarian organizations
- "Corporate" — for-profit companies, startups, scale-ups, VCs, consulting firms
- "Academic" — universities, research institutions, think tanks
- "Student Club" — student organizations, student-run initiatives

For the domain field, just return the company's domain as you know it.
Provide your classification with confidence level."""

    try:
        time.sleep(GPT_DELAY)
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You classify organizations into NGO, Corporate, Academic, or Student Club."},
                {"role": "user", "content": prompt},
            ],
            response_format=AccountEnrichment,
            max_tokens=300,
        )
        result = response.choices[0].message.parsed
        log_api_usage("notion_cleanup", "account_type_classification", "gpt-4o", response.usage, {"company": company_name})
        return result
    except Exception as e:
        console.print(f"[red]GPT-4o classification error for {company_name}: {e}[/red]")
        return None


def update_notion_page(page_id: str, properties: dict) -> bool:
    """Update properties on a Notion page."""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    result = _notion_request("PATCH", url, {"properties": properties})
    return result is not None


def run_phase1_domains(accounts: List[dict]):
    """Phase 1: Populate missing domains and enrich account types."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]Phase 1: Domain Population + Account Type Enrichment[/bold magenta]")
    console.print("=" * 60)

    # Find accounts missing Website URL*
    missing_domain = []
    missing_account_type = []

    for page in accounts:
        website = extract_url_prop(page, "Website URL*")
        account_type = extract_select(page, "Account Type*")
        company_name = extract_title(page)

        if not company_name:
            continue

        if not website:
            missing_domain.append(page)

        if not account_type:
            missing_account_type.append(page)

    console.print(f"[cyan]Accounts missing domain: {len(missing_domain)}[/cyan]")
    console.print(f"[cyan]Accounts missing Account Type*: {len(missing_account_type)}[/cyan]")

    # --- Domain population ---
    domains_populated = 0
    domains_skipped = 0

    for page in missing_domain:
        company_name = extract_title(page)
        page_id = page["id"]
        country = ", ".join(extract_multi_select(page, "Country")) or ""
        city = extract_rich_text(page, "City")

        console.print(f"\n[dim]Resolving: {company_name}[/dim]")

        # Step 1: GPT-4o geographic resolution
        enrichment = resolve_domain_with_gpt(company_name, country, city)
        if not enrichment:
            console.print(f"[yellow]  Skipped (GPT error): {company_name}[/yellow]")
            domains_skipped += 1
            continue

        if enrichment.domain_confidence == "low":
            console.print(f"[yellow]  Low confidence for {company_name}: {enrichment.domain} — skipping[/yellow]")
            domains_skipped += 1
            continue

        guessed_domain = enrichment.domain.lower().strip()

        # Step 2: Verify domain exists
        if not verify_domain_exists(guessed_domain):
            console.print(f"[dim]  Domain {guessed_domain} does not resolve, trying fallback...[/dim]")
            # Step 5: Fallback to find_valid_domain pipeline
            verified = find_valid_domain(company_name, guessed_domain)
            if not verified or not verify_domain_exists(verified):
                console.print(f"[yellow]  Could not verify domain for {company_name}[/yellow]")
                domains_skipped += 1
                continue
            guessed_domain = verified

        # Step 3: Homepage check
        if not verify_company_on_homepage(guessed_domain, company_name):
            console.print(f"[dim]  Homepage check failed for {guessed_domain}, accepting GPT result anyway[/dim]")

        # Step 6: Write verified domain to Notion (stored as full URL)
        full_url = f"https://{guessed_domain}"
        props = {"Website URL*": {"url": full_url}}

        # Also update Account Type* if missing
        account_type = extract_select(page, "Account Type*")
        if not account_type and enrichment.account_type_confidence != "low":
            valid_types = {"NGO", "Corporate", "Academic", "Student Club"}
            if enrichment.account_type in valid_types:
                props["Account Type*"] = {"select": {"name": enrichment.account_type}}
                console.print(f"[green]  Account Type: {enrichment.account_type}[/green]")

        if update_notion_page(page_id, props):
            console.print(f"[green]  ✓ {company_name} → {full_url}[/green]")
            domains_populated += 1
        else:
            console.print(f"[red]  ✗ Failed to update {company_name}[/red]")
            domains_skipped += 1

    # --- Account Type enrichment for accounts that have a domain but no type ---
    types_populated = 0

    # Filter to accounts that have a domain now but still no type
    still_missing_type = []
    for page in missing_account_type:
        account_type = extract_select(page, "Account Type*")
        if account_type:
            continue  # Already set (possibly by domain phase above)
        website = extract_url_prop(page, "Website URL*")
        if website:
            still_missing_type.append(page)

    if still_missing_type:
        console.print(f"\n[cyan]Classifying account types for {len(still_missing_type)} accounts with domains...[/cyan]")

    for page in still_missing_type:
        company_name = extract_title(page)
        page_id = page["id"]
        website = extract_url_prop(page, "Website URL*")
        domain = normalize_domain_from_url(website)

        if not domain:
            continue

        # Fetch homepage content
        homepage_text = fetch_homepage_content(domain)
        if not homepage_text:
            console.print(f"[dim]  No homepage content for {company_name}, skipping type[/dim]")
            continue

        result = classify_account_type_with_homepage(company_name, homepage_text)
        if not result or result.account_type_confidence == "low":
            console.print(f"[yellow]  Low confidence type for {company_name} — skipping[/yellow]")
            continue

        valid_types = {"NGO", "Corporate", "Academic", "Student Club"}
        if result.account_type not in valid_types:
            continue

        props = {"Account Type*": {"select": {"name": result.account_type}}}
        if update_notion_page(page_id, props):
            console.print(f"[green]  ✓ {company_name} → {result.account_type}[/green]")
            types_populated += 1

    # Summary
    console.print(f"\n[bold green]Phase 1 complete: {domains_populated} domains populated, {types_populated} types classified, {domains_skipped} skipped[/bold green]")


# =============================================================================
# Phase 2: Duplicate Detection & Merge
# =============================================================================

def get_status_rank(status: str) -> int:
    """Get rank of a status in the hierarchy. Unknown statuses get -1."""
    try:
        return STATUS_HIERARCHY.index(status)
    except ValueError:
        return -1


def determine_winner(pages: List[dict]) -> Tuple[dict, List[dict]]:
    """
    Determine the merge winner from a group of duplicate pages.

    Winner = highest status rank. Ties broken by: most filled fields → most recent edit.

    Returns:
        (winner_page, list_of_loser_pages)
    """
    def sort_key(page):
        status = extract_status(page)
        rank = get_status_rank(status)
        filled = count_filled_fields(page)
        edited = page.get("last_edited_time", "")
        return (rank, filled, edited)

    sorted_pages = sorted(pages, key=sort_key, reverse=True)
    return sorted_pages[0], sorted_pages[1:]


def build_merge_plan(winner: dict, loser: dict) -> dict:
    """
    Build a merge plan describing what fields to copy from loser to winner.

    Returns a dict with:
        - fields_to_update: {prop_name: new_value_payload} for winner
        - contacts_to_move: list of contact page IDs to re-link
        - meetings_to_move: list of meeting page IDs to re-link
        - suspect_action: "none" | "keep_winner" | "copy_loser" | "promote_swap"
        - contact_to_create: dict with suspect data to promote (if promote_swap)
    """
    plan = {
        "fields_to_update": {},
        "contacts_to_move": [],
        "meetings_to_move": [],
        "suspect_action": "none",
        "contact_to_create": None,
        "field_details": [],  # For display
    }

    winner_props = winner.get("properties", {})
    loser_props = loser.get("properties", {})

    # --- Standard fields: fill winner's empty fields from loser ---
    for prop_name, loser_prop in loser_props.items():
        if prop_name in READ_ONLY_FIELDS:
            continue
        if prop_name in MULTI_SELECT_FIELDS or prop_name in APPEND_TEXT_FIELDS:
            continue  # Handled separately
        if prop_name in RELATION_FIELDS:
            continue  # Handled separately
        if prop_name == OWNER_FIELD:
            continue  # Always keep winner
        if prop_name in SUSPECT_FIELDS:
            continue  # Handled separately

        winner_prop = winner_props.get(prop_name, {})
        ptype = loser_prop.get("type")

        if ptype == "title":
            continue  # Never overwrite title

        winner_empty = _is_prop_empty(winner_prop, ptype)
        loser_empty = _is_prop_empty(loser_prop, ptype)

        if winner_empty and not loser_empty:
            payload = _prop_to_write_payload(loser_prop, ptype)
            if payload is not None:
                plan["fields_to_update"][prop_name] = payload
                plan["field_details"].append(f"{prop_name}: filled from loser")

    # --- Multi-select fields: union ---
    for ms_field in MULTI_SELECT_FIELDS:
        winner_vals = set(extract_multi_select(winner, ms_field))
        loser_vals = set(extract_multi_select(loser, ms_field))
        merged = winner_vals | loser_vals
        if merged and merged != winner_vals:
            plan["fields_to_update"][ms_field] = {
                "multi_select": [{"name": v} for v in sorted(merged)]
            }
            new_vals = loser_vals - winner_vals
            if new_vals:
                plan["field_details"].append(f"{ms_field}: +{', '.join(new_vals)}")

    # --- Append text fields ---
    for tf in APPEND_TEXT_FIELDS:
        winner_text = extract_rich_text(winner, tf)
        loser_text = extract_rich_text(loser, tf)
        if loser_text and loser_text not in (winner_text or ""):
            combined = f"{winner_text}\n{loser_text}".strip() if winner_text else loser_text
            plan["fields_to_update"][tf] = {
                "rich_text": [{"text": {"content": combined[:2000]}}]
            }
            plan["field_details"].append(f"{tf}: appended loser text")

    # --- Relations: move contacts and meetings ---
    loser_contacts = extract_relation_ids(loser, "Contacts")
    winner_contacts = extract_relation_ids(winner, "Contacts")
    new_contacts = [c for c in loser_contacts if c not in winner_contacts]
    if new_contacts:
        plan["contacts_to_move"] = new_contacts
        plan["field_details"].append(f"Contacts: {len(new_contacts)} to re-link")

    loser_meetings = extract_relation_ids(loser, "Previous Meetings")
    winner_meetings = extract_relation_ids(winner, "Previous Meetings")
    new_meetings = [m for m in loser_meetings if m not in winner_meetings]
    if new_meetings:
        plan["meetings_to_move"] = new_meetings
        plan["field_details"].append(f"Meetings: {len(new_meetings)} to re-link")

    # --- Suspect contact handling ---
    winner_has_suspect = bool(extract_rich_text(winner, "[Suspect] Contact Name"))
    loser_has_suspect = bool(extract_rich_text(loser, "[Suspect] Contact Name"))

    if winner_has_suspect and loser_has_suspect:
        # Promote winner's suspect to Contact, then overwrite with loser's suspect
        plan["suspect_action"] = "promote_swap"
        plan["contact_to_create"] = {
            "name": extract_rich_text(winner, "[Suspect] Contact Name"),
            "email": extract_email_prop(winner, "[Suspect] Contact Email") or extract_rich_text(winner, "[Suspect] Contact Email"),
            "phone": extract_phone_prop(winner, "[Suspect] Contact Phone") or extract_rich_text(winner, "[Suspect] Contact Phone"),
            "job_title": extract_rich_text(winner, "[Suspect] Job Title"),
        }
        # Copy loser's suspect fields to winner
        for sf in SUSPECT_FIELDS:
            loser_val = _get_prop_raw(loser_props.get(sf, {}))
            if loser_val is not None:
                plan["fields_to_update"][sf] = loser_val
        plan["field_details"].append("Suspect: promote winner's → Contact, copy loser's")

    elif not winner_has_suspect and loser_has_suspect:
        plan["suspect_action"] = "copy_loser"
        for sf in SUSPECT_FIELDS:
            loser_val = _get_prop_raw(loser_props.get(sf, {}))
            if loser_val is not None:
                plan["fields_to_update"][sf] = loser_val
        plan["field_details"].append("Suspect: copied from loser")

    elif winner_has_suspect and not loser_has_suspect:
        plan["suspect_action"] = "keep_winner"

    return plan


def _is_prop_empty(prop: dict, ptype: str) -> bool:
    """Check if a Notion property value is empty."""
    if not prop:
        return True
    if ptype == "rich_text":
        return not prop.get("rich_text")
    if ptype == "url":
        return not prop.get("url")
    if ptype == "select":
        return not prop.get("select")
    if ptype == "multi_select":
        return not prop.get("multi_select")
    if ptype == "number":
        return prop.get("number") is None
    if ptype == "email":
        return not prop.get("email")
    if ptype == "phone_number":
        return not prop.get("phone_number")
    if ptype == "checkbox":
        return False  # Checkbox is never "empty"
    if ptype == "status":
        return not prop.get("status")
    if ptype == "relation":
        return not prop.get("relation")
    if ptype == "date":
        return not prop.get("date")
    return True


def _prop_to_write_payload(prop: dict, ptype: str) -> Optional[dict]:
    """Convert a Notion property to its write-API payload format."""
    if ptype == "rich_text":
        texts = prop.get("rich_text", [])
        if texts:
            plain = "".join(t.get("plain_text", "") for t in texts)
            return {"rich_text": [{"text": {"content": plain[:2000]}}]}
    elif ptype == "url":
        url_val = prop.get("url")
        if url_val:
            return {"url": url_val}
    elif ptype == "select":
        sel = prop.get("select")
        if sel:
            return {"select": {"name": sel.get("name", "")}}
    elif ptype == "number":
        num = prop.get("number")
        if num is not None:
            return {"number": num}
    elif ptype == "email":
        email_val = prop.get("email")
        if email_val:
            return {"email": email_val}
    elif ptype == "phone_number":
        phone_val = prop.get("phone_number")
        if phone_val:
            return {"phone_number": phone_val}
    elif ptype == "date":
        date_val = prop.get("date")
        if date_val:
            return {"date": date_val}
    elif ptype == "checkbox":
        return {"checkbox": prop.get("checkbox", False)}
    return None


def _get_prop_raw(prop: dict) -> Optional[dict]:
    """Get a property value in write-ready format."""
    ptype = prop.get("type")
    if not ptype:
        return None
    return _prop_to_write_payload(prop, ptype)


def execute_merge(winner: dict, loser: dict, plan: dict) -> bool:
    """Execute a single merge: update winner, re-link relations, archive loser."""
    winner_id = winner["id"]
    loser_id = loser["id"]
    winner_name = extract_title(winner)
    loser_name = extract_title(loser)

    # Pre-execution: verify both pages still exist
    check_winner = _notion_request("GET", f"https://api.notion.com/v1/pages/{winner_id}")
    check_loser = _notion_request("GET", f"https://api.notion.com/v1/pages/{loser_id}")

    if not check_winner or not check_loser:
        console.print(f"[red]  One or both pages no longer exist, skipping merge[/red]")
        return False

    if check_winner.get("archived") or check_loser.get("archived"):
        console.print(f"[red]  One or both pages are archived, skipping merge[/red]")
        return False

    # Step 1: Promote suspect to Contact if needed
    if plan["suspect_action"] == "promote_swap" and plan["contact_to_create"]:
        contact = plan["contact_to_create"]
        if contact["name"]:
            create_contact_in_notion(
                person_name=contact["name"],
                email=contact.get("email", ""),
                account_page_id=winner_id,
            )
            console.print(f"[green]  Promoted suspect '{contact['name']}' to Contact[/green]")

    # Step 2: Update winner's fields
    if plan["fields_to_update"]:
        if update_notion_page(winner_id, plan["fields_to_update"]):
            console.print(f"[green]  Updated {len(plan['fields_to_update'])} fields on winner[/green]")
        else:
            console.print(f"[red]  Failed to update winner fields[/red]")
            return False

    # Step 3: Re-link contacts (update each contact's Accounts relation to point to winner)
    for contact_id in plan["contacts_to_move"]:
        # Fetch current contact to get its existing account relations
        contact_page = _notion_request("GET", f"https://api.notion.com/v1/pages/{contact_id}")
        if not contact_page:
            continue
        existing_accounts = extract_relation_ids(contact_page, "Accounts")
        # Replace loser_id with winner_id
        new_accounts = [a for a in existing_accounts if a != loser_id]
        if winner_id not in new_accounts:
            new_accounts.append(winner_id)
        _notion_request("PATCH", f"https://api.notion.com/v1/pages/{contact_id}", {
            "properties": {
                "Accounts": {"relation": [{"id": a} for a in new_accounts]}
            }
        })

    if plan["contacts_to_move"]:
        console.print(f"[green]  Re-linked {len(plan['contacts_to_move'])} contacts[/green]")

    # Step 4: Re-link meetings
    for meeting_id in plan["meetings_to_move"]:
        meeting_page = _notion_request("GET", f"https://api.notion.com/v1/pages/{meeting_id}")
        if not meeting_page:
            continue
        # Find the relation property that links to accounts
        for prop_name, prop_data in meeting_page.get("properties", {}).items():
            if prop_data.get("type") == "relation":
                rel_ids = [r.get("id", "") for r in prop_data.get("relation", [])]
                if loser_id in rel_ids:
                    new_ids = [r for r in rel_ids if r != loser_id]
                    if winner_id not in new_ids:
                        new_ids.append(winner_id)
                    _notion_request("PATCH", f"https://api.notion.com/v1/pages/{meeting_id}", {
                        "properties": {
                            prop_name: {"relation": [{"id": r} for r in new_ids]}
                        }
                    })
                    break

    if plan["meetings_to_move"]:
        console.print(f"[green]  Re-linked {len(plan['meetings_to_move'])} meetings[/green]")

    # Step 5: Archive loser (soft-delete)
    result = _notion_request("PATCH", f"https://api.notion.com/v1/pages/{loser_id}", {"archived": True})
    if result:
        console.print(f"[green]  Archived loser: {loser_name}[/green]")
    else:
        console.print(f"[red]  Failed to archive loser: {loser_name}[/red]")
        return False

    return True


def log_merge(winner: dict, loser: dict, plan: dict):
    """Log a merge to merge_log.jsonl for recovery."""
    MERGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "winner_id": winner["id"],
        "winner_name": extract_title(winner),
        "loser_id": loser["id"],
        "loser_name": extract_title(loser),
        "suspect_action": plan["suspect_action"],
        "fields_updated": list(plan["fields_to_update"].keys()),
        "contacts_moved": plan["contacts_to_move"],
        "meetings_moved": plan["meetings_to_move"],
        "winner_snapshot": _snapshot_page(winner),
        "loser_snapshot": _snapshot_page(loser),
    }
    with open(MERGE_LOG, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _snapshot_page(page: dict) -> dict:
    """Create a simplified snapshot of a Notion page for logging."""
    snap = {
        "id": page.get("id"),
        "title": extract_title(page),
        "status": extract_status(page),
        "url": page.get("url", ""),
        "website": extract_url_prop(page, "Website URL*"),
        "properties": {},
    }
    for prop_name, prop_data in page.get("properties", {}).items():
        ptype = prop_data.get("type")
        if ptype == "title":
            snap["properties"][prop_name] = extract_title(page)
        elif ptype == "rich_text":
            snap["properties"][prop_name] = extract_rich_text(page, prop_name)
        elif ptype == "url":
            snap["properties"][prop_name] = extract_url_prop(page, prop_name)
        elif ptype == "select":
            snap["properties"][prop_name] = extract_select(page, prop_name)
        elif ptype == "multi_select":
            snap["properties"][prop_name] = extract_multi_select(page, prop_name)
        elif ptype == "status":
            snap["properties"][prop_name] = extract_status(page)
        elif ptype == "number":
            snap["properties"][prop_name] = extract_number(page, prop_name)
        elif ptype == "relation":
            snap["properties"][prop_name] = extract_relation_ids(page, prop_name)
        elif ptype == "email":
            snap["properties"][prop_name] = extract_email_prop(page, prop_name)
        elif ptype == "phone_number":
            snap["properties"][prop_name] = extract_phone_prop(page, prop_name)
    return snap


def run_phase2_merge(accounts: List[dict]):
    """Phase 2: Detect domain-based duplicates and interactively merge."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]Phase 2: Duplicate Detection & Merge[/bold magenta]")
    console.print("=" * 60)

    # Group accounts by normalized domain
    domain_groups: Dict[str, List[dict]] = {}

    for page in accounts:
        website = extract_url_prop(page, "Website URL*")
        domain = normalize_domain_from_url(website)
        if not domain:
            continue
        domain_groups.setdefault(domain, []).append(page)

    # Find groups with duplicates
    duplicate_groups = {d: pages for d, pages in domain_groups.items() if len(pages) > 1}

    if not duplicate_groups:
        console.print("[green]No duplicates found! All accounts have unique domains.[/green]")
        return

    console.print(f"[yellow]Found {len(duplicate_groups)} duplicate domain groups ({sum(len(p) for p in duplicate_groups.values())} accounts total)[/yellow]")

    # Build merge proposals
    proposals = []  # List of (winner, loser, plan, domain)

    for domain, pages in sorted(duplicate_groups.items()):
        winner, losers = determine_winner(pages)
        for loser in losers:
            plan = build_merge_plan(winner, loser)
            proposals.append((winner, loser, plan, domain))

    # Display proposals table
    table = Table(title="Proposed Merges")
    table.add_column("#", style="dim", width=4)
    table.add_column("Winner", style="green", max_width=25)
    table.add_column("Winner Status", max_width=25)
    table.add_column("Loser", style="red", max_width=25)
    table.add_column("Loser Status", max_width=25)
    table.add_column("Fields", style="cyan", width=6)
    table.add_column("Contacts", style="cyan", width=10)
    table.add_column("Suspect", style="yellow", max_width=15)

    for i, (winner, loser, plan, domain) in enumerate(proposals, 1):
        contacts_str = ""
        n_contacts = len(plan["contacts_to_move"])
        if n_contacts:
            contacts_str = f"{n_contacts}→move"

        table.add_row(
            str(i),
            extract_title(winner)[:25],
            extract_status(winner),
            extract_title(loser)[:25],
            extract_status(loser),
            str(len(plan["fields_to_update"])),
            contacts_str,
            plan["suspect_action"],
        )

    console.print(table)

    # Interactive confirmation loop
    while True:
        console.print("\n[bold]Options:[/bold] y=execute all, n=abort, 1/2/...=show detail, s1,3=skip specific")
        choice = input("> ").strip().lower()

        if choice == "n":
            console.print("[yellow]Merge aborted.[/yellow]")
            return

        if choice == "y":
            break

        if choice.startswith("s"):
            # Parse skip indices
            skip_str = choice[1:]
            skip_indices = set()
            for part in skip_str.split(","):
                part = part.strip()
                if part.isdigit():
                    skip_indices.add(int(part))
            proposals = [p for i, p in enumerate(proposals, 1) if i not in skip_indices]
            console.print(f"[cyan]Skipped {len(skip_indices)} merges, {len(proposals)} remaining.[/cyan]")
            break

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(proposals):
                winner, loser, plan, domain = proposals[idx - 1]
                console.print(f"\n[bold]Merge #{idx} Detail — Domain: {domain}[/bold]")
                console.print(f"  Winner: {extract_title(winner)} (status: {extract_status(winner)}, fields: {count_filled_fields(winner)})")
                console.print(f"  Loser:  {extract_title(loser)} (status: {extract_status(loser)}, fields: {count_filled_fields(loser)})")
                console.print(f"  Suspect action: {plan['suspect_action']}")
                if plan["field_details"]:
                    console.print("  Changes:")
                    for detail in plan["field_details"]:
                        console.print(f"    - {detail}")
                else:
                    console.print("  No field changes needed")
            else:
                console.print("[yellow]Invalid index[/yellow]")
            continue

        console.print("[yellow]Invalid choice[/yellow]")

    # Execute merges — re-fetch winner before each merge so that sequential
    # merges for the same domain accumulate correctly (e.g. 10 Corps Africa losers).
    console.print(f"\n[bold cyan]Executing {len(proposals)} merges...[/bold cyan]")
    success = 0
    failed = 0
    refreshed_winners: dict = {}  # winner_id → latest page data

    for i, (winner, loser, _stale_plan, domain) in enumerate(proposals, 1):
        winner_id = winner["id"]
        loser_name = extract_title(loser)

        # Re-fetch winner from Notion (may have been enriched by a prior merge)
        if winner_id in refreshed_winners:
            fresh_winner = refreshed_winners[winner_id]
        else:
            fresh_winner = _notion_request("GET", f"https://api.notion.com/v1/pages/{winner_id}")
            if not fresh_winner:
                console.print(f"[red]  Could not re-fetch winner, using stale data[/red]")
                fresh_winner = winner

        winner_name = extract_title(fresh_winner)
        console.print(f"\n[cyan]Merge {i}/{len(proposals)}: {winner_name} ← {loser_name}[/cyan]")

        # Rebuild merge plan from fresh winner state
        plan = build_merge_plan(fresh_winner, loser)

        # Log before executing (for recovery)
        log_merge(fresh_winner, loser, plan)

        if execute_merge(fresh_winner, loser, plan):
            success += 1
            console.print(f"[green]  ✓ Merge successful[/green]")
            # Cache the updated winner for next iteration
            updated = _notion_request("GET", f"https://api.notion.com/v1/pages/{winner_id}")
            if updated:
                refreshed_winners[winner_id] = updated
        else:
            failed += 1
            console.print(f"[red]  ✗ Merge failed[/red]")

    console.print(f"\n[bold green]Phase 2 complete: {success} merged, {failed} failed[/bold green]")
    if success > 0:
        console.print(f"[dim]Merge log saved to: {MERGE_LOG}[/dim]")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Notion Cleanup Agent — Domain population & duplicate merging")
    parser.add_argument("--domains", action="store_true", help="Phase 1: Populate missing domains + account types")
    parser.add_argument("--merge", action="store_true", help="Phase 2: Detect and merge duplicates")
    parser.add_argument("--all", action="store_true", help="Run both phases (default)")
    args = parser.parse_args()

    # Default to --all if no flag specified
    if not args.domains and not args.merge:
        args.all = True

    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Social AI — Notion Cleanup Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print("=" * 60)

    # Validate config
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not set in .env[/red]")
        return
    if not NOTION_DB_ACCOUNTS_ID:
        console.print("[red]Error: NOTION_DB_ACCOUNTS_ID not set in .env[/red]")
        return

    # Fetch all accounts once (shared between phases)
    accounts = fetch_all_accounts()
    if not accounts:
        console.print("[red]No accounts found in Notion database[/red]")
        return

    if args.domains or args.all:
        run_phase1_domains(accounts)

        if args.all:
            # Re-fetch accounts after domain updates for accurate merge detection
            console.print("\n[dim]Re-fetching accounts after domain updates...[/dim]")
            accounts = fetch_all_accounts()

    if args.merge or args.all:
        run_phase2_merge(accounts)

    console.print("\n[bold green]Notion Cleanup Agent finished.[/bold green]")


if __name__ == "__main__":
    main()
