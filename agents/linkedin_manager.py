"""
LinkedIn Manager — Orchestrator for LinkedIn outreach analysis.

Parses saved connections HTML, cross-references Notion contacts/accounts,
detects new connections, identifies follow-up needs, marks ghosted leads,
and emails a report.

Usage:
    python -m agents.linkedin_manager
    python -m agents.linkedin_manager --dry-run
"""
from __future__ import annotations

import argparse
import csv
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Set

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
    GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD,
    REPORT_RECIPIENT_EMAIL,
)
from utils.api_logger import log_api_usage
from utils.notion_client import _notion_api_headers
from agents.linkedin_parser import (
    ParsedConnection,
    parse_connections,
    _normalize_linkedin_url,
)
from agents.report_generator import ActionItem, generate_linkedin_report, generate_email_html
from agents.notion_cleanup import STATUS_HIERARCHY

console = Console()

# Rate limiting
NOTION_DELAY = 0.35  # seconds between Notion API calls

# Thresholds (days)
FOLLOW_UP_DAYS = 3
GHOSTED_DAYS = 10


class LinkedInManagerError(RuntimeError):
    """Raised when the LinkedIn manager cannot safely complete a run."""


# =============================================================================
# Pydantic Models for GPT-4o Structured Output
# =============================================================================

class FollowUpDraft(BaseModel):
    """GPT-4o output for follow-up message drafting."""
    message: str = Field(description="The follow-up message text (max 300 chars)")
    approach: str = Field(description="Brief description of the follow-up approach used")


# =============================================================================
# Status Guard
# =============================================================================

def _get_status_rank(status: str) -> int:
    """Get rank of a status in the hierarchy. Unknown statuses get -1."""
    try:
        return STATUS_HIERARCHY.index(status)
    except ValueError:
        return -1


def _should_update_status(current_status: str, new_status: str) -> bool:
    """Return True only if new_status is a higher rank (upgrade), with specific exceptions like Nurture."""
    # Exception: Ghosting moves lead to Nurture
    if new_status == "Nurture" and current_status in ("Contacted LinkedIn \U0001f310", "Contacted Mail \U0001f4e9"):
        return True

    current_rank = _get_status_rank(current_status)
    new_rank = _get_status_rank(new_status)
    if new_rank < 0:
        return False  # Unknown new status
    if current_rank < 0:
        console.print(f"[yellow]  ⚠ Unrecognized status '{current_status}' — skipping update to avoid accidental downgrade[/yellow]")
        return False  # Unknown current status — refuse update to prevent accidental downgrade
    return new_rank > current_rank


# =============================================================================
# Notion API Operations
# =============================================================================

def _notion_get(url: str) -> Optional[dict]:
    """GET request to Notion API with rate limiting."""
    time.sleep(NOTION_DELAY)
    headers = _notion_api_headers()
    try:
        resp = http_requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 502, 503):
            time.sleep(2)
            resp = http_requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        console.print(f"[red]Notion GET {resp.status_code}: {resp.json().get('message', '')[:100]}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]Notion GET error: {e}[/red]")
        return None


def _notion_post(url: str, body: dict) -> Optional[dict]:
    """POST request to Notion API with rate limiting."""
    time.sleep(NOTION_DELAY)
    headers = _notion_api_headers()
    try:
        resp = http_requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 502, 503):
            time.sleep(2)
            resp = http_requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        console.print(f"[red]Notion POST {resp.status_code}: {resp.json().get('message', '')[:100]}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]Notion POST error: {e}[/red]")
        return None


def _notion_patch(url: str, body: dict) -> Optional[dict]:
    """PATCH request to Notion API with rate limiting."""
    time.sleep(NOTION_DELAY)
    headers = _notion_api_headers()
    try:
        resp = http_requests.patch(url, headers=headers, json=body, timeout=30)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (429, 502, 503):
            time.sleep(2)
            resp = http_requests.patch(url, headers=headers, json=body, timeout=30)
            if resp.status_code == 200:
                return resp.json()
        console.print(f"[red]Notion PATCH {resp.status_code}: {resp.json().get('message', '')[:100]}[/red]")
        return None
    except Exception as e:
        console.print(f"[red]Notion PATCH error: {e}[/red]")
        return None


# =============================================================================
# Notion Contact/Account Fetching
# =============================================================================

def fetch_all_contacts() -> List[dict]:
    """Paginated query of the Contacts database."""
    if not NOTION_TOKEN or not NOTION_DB_CONTACTS_ID:
        raise LinkedInManagerError("NOTION_TOKEN or NOTION_DB_CONTACTS_ID not configured")

    url = f"https://api.notion.com/v1/databases/{NOTION_DB_CONTACTS_ID}/query"
    results = []
    start_cursor = None

    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = _notion_post(url, body)
        if not data:
            raise LinkedInManagerError(
                f"Could not query Notion Contacts database {NOTION_DB_CONTACTS_ID}. "
                "Check the database ID and share/access for the Notion integration."
            )

        results.extend(data.get("results", []))
        if not data.get("has_more", False):
            break
        start_cursor = data.get("next_cursor")

    console.print(f"[cyan]Fetched {len(results)} contacts from Notion[/cyan]")
    return results


def fetch_all_accounts() -> List[dict]:
    """Paginated query of the Accounts database."""
    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        raise LinkedInManagerError("NOTION_TOKEN or NOTION_DB_ACCOUNTS_ID not configured")

    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"
    results = []
    start_cursor = None

    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        data = _notion_post(url, body)
        if not data:
            raise LinkedInManagerError(
                f"Could not query Notion Accounts database {NOTION_DB_ACCOUNTS_ID}. "
                "Check the database ID and share/access for the Notion integration."
            )

        results.extend(data.get("results", []))
        if not data.get("has_more", False):
            break
        start_cursor = data.get("next_cursor")

    console.print(f"[cyan]Fetched {len(results)} accounts from Notion[/cyan]")
    return results


def _text_from_property(prop_data: dict) -> str:
    """Extract plain text from common Notion property shapes."""
    prop_type = prop_data.get("type")
    if prop_type == "rich_text":
        return "".join(t.get("plain_text", "") for t in prop_data.get("rich_text", []))
    if prop_type == "title":
        return "".join(t.get("plain_text", "") for t in prop_data.get("title", []))
    if prop_type == "formula":
        formula = prop_data.get("formula") or {}
        if formula.get("type") == "string":
            return formula.get("string") or ""
    if prop_type == "url":
        return prop_data.get("url") or ""
    return ""


def _first_property_text(props: dict, names: List[str]) -> str:
    for name in names:
        value = _text_from_property(props.get(name, {})).strip()
        if value:
            return value
    return ""


def build_linkedin_lookup(contacts: List[dict]) -> Dict[str, dict]:
    """Build a dict mapping normalized LinkedIn URL -> {contact_id, contact_name, account_ids}."""
    lookup = {}

    for page in contacts:
        props = page.get("properties", {})
        contact_id = page.get("id", "")

        # Extract LinkedIn URL
        linkedin_prop = props.get("LinkedIn", {})
        linkedin_url = ""
        if linkedin_prop.get("type") == "url":
            linkedin_url = linkedin_prop.get("url") or ""

        if not linkedin_url:
            continue

        normalized = _normalize_linkedin_url(linkedin_url)
        if not normalized:
            continue

        # Extract Account relation IDs
        accounts_prop = props.get("Accounts", {})
        account_ids = []
        if accounts_prop.get("type") == "relation":
            account_ids = [r.get("id", "") for r in accounts_prop.get("relation", []) if r.get("id")]

        # Extract contact name
        name = ""
        for prop_data in props.values():
            if prop_data.get("type") == "title":
                title_arr = prop_data.get("title", [])
                if title_arr:
                    name = title_arr[0].get("plain_text", "")
                break

        lookup[normalized] = {
            "contact_id": contact_id,
            "contact_name": name,
            "account_ids": account_ids,
        }

    console.print(f"[cyan]Built LinkedIn lookup: {len(lookup)} contacts with URLs[/cyan]")
    return lookup


def build_account_linkedin_lookup(accounts: List[dict]) -> Dict[str, dict]:
    """Build a LinkedIn lookup directly from Accounts DB contact fields."""
    lookup = {}

    for page in accounts:
        props = page.get("properties", {})
        account_id = page.get("id", "")

        linkedin_url = _first_property_text(props, ["[Suspect] Contact LinkedIn URL", "LinkedIn"])
        normalized = _normalize_linkedin_url(linkedin_url)
        if not normalized:
            continue

        contact_name = _first_property_text(
            props,
            ["[Suspect] Contact Name", "Main Contact First Name", "Main Contact Last Name"],
        )
        if not contact_name:
            first_name = _first_property_text(props, ["Main Contact First Name"])
            last_name = _first_property_text(props, ["Main Contact Last Name"])
            contact_name = f"{first_name} {last_name}".strip()

        account_name = _first_property_text(props, ["Organization*", "Cleaned Name*"])

        lookup[normalized] = {
            "contact_id": "",
            "contact_name": contact_name or account_name,
            "account_ids": [account_id] if account_id else [],
            "cold_message": _first_property_text(props, ["LinkedIn 1st Cold"]),
            "fu_message": _first_property_text(props, ["LinkedIn FU message"]),
        }

    console.print(f"[cyan]Built LinkedIn lookup: {len(lookup)} accounts with contact URLs[/cyan]")
    return lookup


def fetch_account_page(account_id: str) -> Optional[dict]:
    """Fetch a single Account page and extract key fields including last_edited_time."""
    data = _notion_get(f"https://api.notion.com/v1/pages/{account_id}")
    if not data:
        return None

    props = data.get("properties", {})

    # Extract status
    status = ""
    status_prop = props.get("Status", {})
    if status_prop.get("type") == "status" and status_prop.get("status"):
        status = status_prop["status"].get("name", "")

    # Extract organization name
    org_name = ""
    for prop_data in props.values():
        if prop_data.get("type") == "title":
            title_arr = prop_data.get("title", [])
            if title_arr:
                org_name = title_arr[0].get("plain_text", "")
            break

    # Extract Campaign ID (multi_select)
    campaign_ids = []
    campaign_prop = props.get("Campaign ID", {})
    if campaign_prop.get("type") == "multi_select":
        campaign_ids = [ms.get("name", "") for ms in campaign_prop.get("multi_select", [])]

    # Extract Date Contacted LinkedIn
    date_contacted_str = ""
    date_contacted_prop = props.get("Date Contacted LinkedIn", {})
    if date_contacted_prop.get("type") == "date" and date_contacted_prop.get("date"):
        date_contacted_str = date_contacted_prop["date"].get("start", "")

    date_contacted = None
    if date_contacted_str:
        try:
            date_contacted = datetime.fromisoformat(date_contacted_str.replace("Z", "+00:00"))
            if date_contacted.tzinfo is None:
                date_contacted = date_contacted.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    # Extract last_edited_time (top-level page metadata) as fallback
    last_edited_str = data.get("last_edited_time", "")
    last_edited = None
    if last_edited_str:
        try:
            last_edited = datetime.fromisoformat(last_edited_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    return {
        "page_id": account_id,
        "status": status,
        "organization": org_name,
        "campaign_ids": campaign_ids,
        "last_edited_time": last_edited,
        "date_contacted_linkedin": date_contacted,
    }


def fetch_contact_outreach(contact_id: str) -> dict:
    """Fetch outreach messages from a Contact page."""
    data = _notion_get(f"https://api.notion.com/v1/pages/{contact_id}")
    if not data:
        return {"cold_message": "", "fu_message": ""}

    props = data.get("properties", {})

    cold_msg = ""
    cold_prop = props.get("LinkedIn 1st Cold", {})
    if cold_prop.get("type") == "rich_text":
        texts = cold_prop.get("rich_text", [])
        if texts:
            cold_msg = "".join(t.get("plain_text", "") for t in texts)

    fu_msg = ""
    fu_prop = props.get("LinkedIn FU message", {})
    if fu_prop.get("type") == "rich_text":
        texts = fu_prop.get("rich_text", [])
        if texts:
            fu_msg = "".join(t.get("plain_text", "") for t in texts)

    return {"cold_message": cold_msg, "fu_message": fu_msg}


def update_account_status(account_id: str, status: str, update_contacted_date: bool = False) -> bool:
    """Update the Status property on an Account page."""
    properties = {"Status": {"status": {"name": status}}}
    if update_contacted_date:
        properties["Date Contacted LinkedIn"] = {"date": {"start": datetime.now(timezone.utc).isoformat()}}

    result = _notion_patch(
        f"https://api.notion.com/v1/pages/{account_id}",
        {"properties": properties}
    )
    return result is not None


# =============================================================================
# GPT-4o Follow-Up Drafting
# =============================================================================

def draft_follow_up(
    partner_name: str,
    account_name: str,
    cold_message: str = "",
    fu_message: str = "",
) -> Optional[FollowUpDraft]:
    """Use GPT-4o to draft a follow-up message based on outreach context."""
    if not OPENAI_API_KEY:
        return None

    client = OpenAI(api_key=OPENAI_API_KEY, timeout=180.0, max_retries=4)

    context_parts = []
    if cold_message:
        context_parts.append(f"Original cold message: {cold_message[:300]}")
    if fu_message:
        context_parts.append(f"Previous follow-up on file: {fu_message[:300]}")
    context = "\n".join(context_parts) if context_parts else "No original messages on file."

    prompt = f"""Draft a short LinkedIn follow-up message for {partner_name} at {account_name or 'their company'}.

CONTEXT:
{context}
- They connected but haven't replied to the initial outreach.
- It's been 3+ days since the last message.

RULES:
- Keep it under 300 characters
- Be warm but professional
- CRITICAL: The follow-up MUST continue the same hook/topic from the original cold message. If the cold message was about talent/hiring, follow up on that. If it was about their sustainability work, follow up on that. Never switch to an unrelated topic.
- Reference what you specifically said in the cold message
- Add one new supporting data point that reinforces the same angle
- Don't be pushy or guilt-trippy
- End with a soft call to action (question or suggestion)
- Write in English unless the original message was in German (then write in German)

ABSOLUTE PROHIBITIONS — DO NOT VIOLATE:
- NEVER invent facts, achievements, awards, projects, or partnerships that are not explicitly mentioned in the CONTEXT above
- NEVER claim we received awards, started new projects, or have partnerships that aren't in the cold message
- Only reference things that were ALREADY mentioned in the original cold message
- If you don't have enough context, keep it simple and short — a brief nudge is better than a fabricated story"""

    try:
        response = client.beta.chat.completions.parse(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You write concise, warm LinkedIn follow-up messages for B2B outreach. NEVER invent or fabricate facts, projects, awards, or partnerships. Only use information explicitly provided in the context."},
                {"role": "user", "content": prompt},
            ],
            response_format=FollowUpDraft,
            max_tokens=200,
        )
        result = response.choices[0].message.parsed
        log_api_usage("linkedin_manager", "follow_up_draft", "gpt-4o", response.usage, {"partner": partner_name})
        return result
    except Exception as e:
        console.print(f"[red]GPT-4o follow-up error for {partner_name}: {e}[/red]")
        return None


# =============================================================================
# Logic Rules
# =============================================================================

def apply_logic(
    connections: List[ParsedConnection],
    linkedin_lookup: Dict[str, dict],
    dry_run: bool = False,
    campaigns: Optional[List[str]] = None,
) -> List[ActionItem]:
    """
    Apply outreach logic rules:
    A — New Connections: connect request accepted (from HTML)
    B — Follow-Ups: contacted 3+ days ago with no progress (from Notion)
    C — Ghosted: contacted 10+ days ago with no progress (from Notion)

    If campaigns is set, only process accounts belonging to those Campaign IDs.
    """
    actions: List[ActionItem] = []
    gpt_calls = 0
    accounts_checked = 0

    # Build set of connected profile URLs from HTML
    connections_set: Set[str] = {
        _normalize_linkedin_url(c.profile_url)
        for c in connections
        if c.profile_url
    }

    # Track which accounts we've already processed
    processed_accounts: Set[str] = set()

    now = datetime.now(timezone.utc)

    for normalized_url, contact_info in linkedin_lookup.items():
        account_ids = contact_info.get("account_ids", [])
        if not account_ids:
            continue

        account_id = account_ids[0]
        if account_id in processed_accounts:
            continue
        processed_accounts.add(account_id)

        try:
            account = fetch_account_page(account_id)
            if not account:
                continue
        except Exception as e:
            console.print(f"[red]Error fetching account for {contact_info.get('contact_name', '?')}: {e}[/red]")
            continue

        accounts_checked += 1
        current_status = account.get("status", "")
        account_name = account.get("organization", "")
        contact_name = contact_info.get("contact_name", "")
        contact_id = contact_info.get("contact_id", "")

        # Campaign filter: skip accounts not in the requested campaigns
        if campaigns:
            account_campaigns = account.get("campaign_ids", [])
            if not any(c in campaigns for c in account_campaigns):
                continue

        # --- Rule A: New Connections ---
        # Trigger if person is now in connections AND status < "Contacted LinkedIn 🌐"
        # This covers: Suspect, Prospect Qualified, Connect. Request sent
        current_status_rank = _get_status_rank(current_status)
        contacted_linkedin_rank = _get_status_rank("Contacted LinkedIn \U0001f310")

        if normalized_url in connections_set and current_status_rank >= 0 and current_status_rank < contacted_linkedin_rank:
            new_status = "Contacted LinkedIn \U0001f310"
            if _should_update_status(current_status, new_status):
                console.print(f"[green]  A: {contact_name} — connected! {current_status} -> {new_status}[/green]")

                # Fetch outreach context from contact
                outreach = fetch_contact_outreach(contact_id) if contact_id else {
                    "cold_message": contact_info.get("cold_message", ""),
                    "fu_message": contact_info.get("fu_message", ""),
                }

                actions.append(ActionItem(
                    category="new_connection",
                    partner_name=contact_name,
                    profile_url=normalized_url,
                    account_name=account_name,
                    old_status=current_status,
                    new_status=new_status,
                    cold_message=outreach.get("cold_message", ""),
                    fu_message=outreach.get("fu_message", ""),
                    account_id=account_id,
                    update_contacted_date=True,
                ))
            continue  # Don't apply other rules to fresh connections

        # --- Rules B & C: Follow-Ups and Ghosted (Notion-driven) ---
        if current_status == "Contacted LinkedIn \U0001f310":
            ref_date = account.get("date_contacted_linkedin") or account.get("last_edited_time")
            if not ref_date:
                continue

            days_since = (now - ref_date).days

            if days_since >= GHOSTED_DAYS:
                # Rule C: Ghosted (move to Email after ghosting 1st & 2nd LinkedIn msgs)
                new_status = "Contacted Mail \U0001f4e9"
                if _should_update_status(current_status, new_status):
                    actions.append(ActionItem(
                        category="ghosted",
                        partner_name=contact_name,
                        profile_url=normalized_url,
                        account_name=account_name,
                        old_status=current_status,
                        new_status=new_status,
                        reasoning=f"No response for {days_since} days after LinkedIn outreach",
                        account_id=account_id,
                    ))
                    console.print(f"[red]  C: {contact_name} — ghosted ({days_since}d)[/red]")

            elif days_since >= FOLLOW_UP_DAYS:
                # Rule B: Follow-Up needed
                outreach = fetch_contact_outreach(contact_id) if contact_id else {
                    "cold_message": contact_info.get("cold_message", ""),
                    "fu_message": contact_info.get("fu_message", ""),
                }
                cold_msg = outreach.get("cold_message", "")
                fu_msg = outreach.get("fu_message", "")

                # Use the copywriter's pre-written FU message if available.
                # Only fall back to GPT-4o draft if no FU message exists in Notion.
                draft_msg = ""
                approach = ""
                if fu_msg:
                    draft_msg = fu_msg
                    approach = "using copywriter FU message from Notion"
                    console.print(f"[dim]    Using existing FU message from Notion[/dim]")
                elif not dry_run:
                    draft = draft_follow_up(
                        partner_name=contact_name,
                        account_name=account_name,
                        cold_message=cold_msg,
                        fu_message=fu_msg,
                    )
                    gpt_calls += 1
                    if draft:
                        draft_msg = draft.message
                        approach = draft.approach

                actions.append(ActionItem(
                    category="follow_up",
                    partner_name=contact_name,
                    profile_url=normalized_url,
                    account_name=account_name,
                    old_status=current_status,
                    new_status=current_status,
                    draft_message=draft_msg,
                    reasoning=f"{days_since} days since last activity" + (f" — {approach}" if approach else ""),
                    cold_message=cold_msg,
                    fu_message=fu_msg,
                ))
                console.print(f"[yellow]  B: {contact_name} — follow-up needed ({days_since}d)[/yellow]")

        elif current_status == "Contacted Mail \U0001f4e9":
            ref_date = account.get("last_edited_time")
            if not ref_date:
                continue

            days_since = (now - ref_date).days

            if days_since >= GHOSTED_DAYS:
                # Rule D: Email Ghosted -> Nurture
                new_status = "Nurture"
                if _should_update_status(current_status, new_status):
                    actions.append(ActionItem(
                        category="ghosted",
                        partner_name=contact_name,
                        profile_url=normalized_url,
                        account_name=account_name,
                        old_status=current_status,
                        new_status=new_status,
                        reasoning=f"No response for {days_since} days after Email outreach",
                        account_id=account_id,
                    ))
                    console.print(f"[red]  D: {contact_name} — email ghosted ({days_since}d), moving to Nurture[/red]")

    return actions


# =============================================================================
# CSV Export
# =============================================================================

def export_actions_to_csv(actions: List[ActionItem]) -> Path:
    """Export actionable items to CSV for easy copy-paste outreach."""
    from utils.config import REPORTS_DIR

    timestamp = datetime.now().strftime("%Y%m%d")
    csv_path = REPORTS_DIR / f"linkedin_outreach_actions_{timestamp}.csv"

    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)

        # Header row
        writer.writerow([
            "Contact Name",
            "LinkedIn URL",
            "Company",
            "Action Type",
            "Next Message",
            "Status"
        ])

        # Data rows - only include actionable items (new_connection, follow_up)
        for action in actions:
            if action.category in ("new_connection", "follow_up"):
                # Determine next message — flatten to single line for easy copy-paste
                if action.category == "follow_up" and action.draft_message:
                    next_message = action.draft_message
                elif action.cold_message:
                    next_message = action.cold_message
                elif action.fu_message:
                    next_message = action.fu_message
                else:
                    next_message = "[No message on file]"

                # Preserve line breaks for easy copy-paste into LinkedIn chat
                next_message = next_message.strip()

                action_type = {
                    "new_connection": "New Connection (send 1st message)",
                    "follow_up": "Follow-Up (send 2nd message)"
                }.get(action.category, action.category)

                writer.writerow([
                    action.partner_name,
                    action.profile_url if action.profile_url.startswith("http") else f"https://linkedin.com{action.profile_url}",
                    action.account_name,
                    action_type,
                    next_message,
                    f"{action.old_status} → {action.new_status}"
                ])

    console.print(f"[green]  CSV exported: {csv_path}[/green]")
    return csv_path


# =============================================================================
# Email Delivery
# =============================================================================

def send_email_report(actions: List[ActionItem], pdf_path: Path, csv_path: Optional[Path] = None, stats: Optional[dict] = None) -> bool:
    """Send the outreach report via Gmail."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        console.print("[yellow]Email not configured (GMAIL_ADDRESS / GMAIL_APP_PASSWORD missing). Skipping email.[/yellow]")
        return False

    recipient = REPORT_RECIPIENT_EMAIL or GMAIL_ADDRESS

    # Count by category
    follow_ups = sum(1 for a in actions if a.category == "follow_up")
    new_conns = sum(1 for a in actions if a.category == "new_connection")
    ghosted = sum(1 for a in actions if a.category == "ghosted")

    subject = f"LinkedIn Agent — {new_conns} new connections, {follow_ups} follow-ups, {ghosted} ghosted"
    if not actions:
        subject = "LinkedIn Agent — No actions this week"

    # Build email
    msg = MIMEMultipart()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = recipient
    msg["Subject"] = subject

    # HTML body
    html_body = generate_email_html(actions, stats)
    msg.attach(MIMEText(html_body, "html"))

    # Attach PDF
    if pdf_path.exists():
        with open(pdf_path, "rb") as f:
            pdf_attachment = MIMEApplication(f.read(), _subtype="pdf")
            pdf_attachment.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
            msg.attach(pdf_attachment)

    # Attach CSV if provided
    if csv_path and csv_path.exists():
        with open(csv_path, "rb") as f:
            csv_attachment = MIMEApplication(f.read(), _subtype="csv")
            csv_attachment.add_header("Content-Disposition", "attachment", filename=csv_path.name)
            msg.attach(csv_attachment)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        console.print(f"[green]Email sent to {recipient}[/green]")
        return True
    except Exception as e:
        console.print(f"[red]Email sending failed: {e}[/red]")
        return False


# =============================================================================
# Orchestrator
# =============================================================================

def run_linkedin_manager(
    dry_run: bool = False,
    campaigns: Optional[List[str]] = None,
    connections_path: Optional[Path] = None,
):
    """Run the full LinkedIn analysis pipeline."""
    console.print("\n" + "=" * 60)
    console.print("[bold magenta]TUM Social AI — LinkedIn Analyst Agent[/bold magenta]")
    console.print(f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    if dry_run:
        console.print("[yellow]DRY RUN — no Notion updates, no email[/yellow]")
    console.print("=" * 60)

    # Validate config
    if not OPENAI_API_KEY:
        raise LinkedInManagerError("OPENAI_API_KEY not set in .env")
    if not NOTION_TOKEN:
        raise LinkedInManagerError("NOTION_TOKEN not set in .env")
    if not NOTION_DB_CONTACTS_ID:
        raise LinkedInManagerError("NOTION_DB_CONTACTS_ID not set in .env")
    if not NOTION_DB_ACCOUNTS_ID:
        raise LinkedInManagerError("NOTION_DB_ACCOUNTS_ID not set in .env")

    # Step 1: Parse connections HTML
    console.print("\n[cyan]Step 1: Parsing connections HTML...[/cyan]")
    connections, parse_warnings = parse_connections(connections_path=connections_path)

    if parse_warnings:
        for w in parse_warnings:
            console.print(f"  [yellow]Warning: {w}[/yellow]")

    console.print(f"  Connections parsed: {len(connections)}")
    if not connections:
        raise LinkedInManagerError("No LinkedIn connections could be parsed from the saved HTML")

    # Step 2: Fetch Notion contacts and build lookup
    console.print("\n[cyan]Step 2: Fetching Notion contacts...[/cyan]")
    lookup_source = "Contacts"
    try:
        contacts = fetch_all_contacts()
        linkedin_lookup = build_linkedin_lookup(contacts)
    except LinkedInManagerError as e:
        console.print(f"[yellow]Contacts DB unavailable: {e}[/yellow]")
        console.print("[yellow]Falling back to Accounts DB contact fields...[/yellow]")
        lookup_source = "Accounts"
        accounts = fetch_all_accounts()
        linkedin_lookup = build_account_linkedin_lookup(accounts)

    if not linkedin_lookup:
        if lookup_source == "Contacts":
            console.print("[yellow]No LinkedIn URLs found in Contacts DB; trying Accounts DB contact fields...[/yellow]")
            lookup_source = "Accounts"
            accounts = fetch_all_accounts()
            linkedin_lookup = build_account_linkedin_lookup(accounts)

    if not linkedin_lookup:
        raise LinkedInManagerError("No LinkedIn URLs found in Notion Contacts or Accounts")

    # Step 3: Apply logic rules
    if campaigns:
        console.print(f"\n[cyan]Step 3: Applying outreach logic rules (campaigns: {', '.join(campaigns)})...[/cyan]")
    else:
        console.print("\n[cyan]Step 3: Applying outreach logic rules...[/cyan]")
    actions = apply_logic(connections, linkedin_lookup, dry_run=dry_run, campaigns=campaigns)
    console.print(f"  Actions generated: {len(actions)}")

    # Step 4: Generate PDF report
    console.print("\n[cyan]Step 4: Generating PDF report...[/cyan]")
    stats = {
        "connections_parsed": len(connections),
        "contacts_in_notion": len(linkedin_lookup),
        "accounts_checked": len({ci["account_ids"][0] for ci in linkedin_lookup.values() if ci.get("account_ids")}),
        "gpt_calls": sum(1 for a in actions if a.category == "follow_up" and a.draft_message),
        "lookup_source": lookup_source,
    }
    pdf_path = generate_linkedin_report(actions, stats)
    console.print(f"[green]  Report saved: {pdf_path}[/green]")

    # Step 4.5: Export actions to CSV
    csv_path = None
    if actions:
        console.print("\n[cyan]Step 4.5: Exporting actionable items to CSV...[/cyan]")
        csv_path = export_actions_to_csv(actions)

    # Step 5: Send email & Update Notion
    if not dry_run:
        console.print("\n[cyan]Step 5: Sending email report...[/cyan]")
        email_success = send_email_report(actions, pdf_path, csv_path, stats)
        
        if email_success:
            console.print("\n[cyan]Step 6: Updating Notion database...[/cyan]")
            updates_made = 0
            for action in actions:
                if action.account_id and action.old_status != action.new_status:
                    update_account_status(
                        action.account_id, 
                        action.new_status, 
                        update_contacted_date=action.update_contacted_date
                    )
                    updates_made += 1
            console.print(f"[green]  {updates_made} accounts updated in Notion![/green]")
        else:
            console.print("\n[red]Step 6: Skipping Notion updates because email report failed![/red]")
            # Send an error email
            try:
                import smtplib
                from email.mime.text import MIMEText
                
                err_msg = MIMEText("The LinkedIn Agent encountered an error while sending the outreach report. As a security measure, NO lead statuses have been updated in Notion.\n\nPlease check the agent logs.", "plain")
                err_msg["Subject"] = "ERROR: LinkedIn Agent Failed to Send Report"
                err_msg["From"] = GMAIL_ADDRESS
                err_msg["To"] = REPORT_RECIPIENT_EMAIL or GMAIL_ADDRESS
                
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                    server.send_message(err_msg)
            except Exception as outer_e:
                console.print(f"[red]Could not even send the error email: {outer_e}[/red]")
            
    else:
        console.print("\n[dim]Step 5/6: Skipping email and Notion updates (dry run)[/dim]")

    # Step 7: Rich summary table
    if actions:
        table = Table(title="Outreach Actions Summary")
        table.add_column("Category", style="bold", max_width=15)
        table.add_column("Contact", max_width=25)
        table.add_column("Account", max_width=25)
        table.add_column("Status Change", max_width=35)
        table.add_column("Detail", max_width=40)

        for a in actions:
            style = {
                "follow_up": "yellow",
                "new_connection": "cyan",
                "ghosted": "red",
            }.get(a.category, "")

            status_change = f"{a.old_status} -> {a.new_status}" if a.old_status != a.new_status else a.old_status
            if a.draft_message:
                detail = a.draft_message[:40]
            elif a.cold_message:
                detail = a.cold_message[:40]
            elif a.reasoning:
                detail = a.reasoning[:40]
            else:
                detail = ""

            table.add_row(
                f"[{style}]{a.category}[/{style}]",
                a.partner_name[:25],
                a.account_name[:25],
                status_change,
                detail,
            )

        console.print(table)

    console.print(f"\n[bold green]LinkedIn Analyst Agent finished. {len(actions)} actions processed.[/bold green]")


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TUM Social AI — LinkedIn Analyst Agent")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without updating Notion or sending email")
    parser.add_argument("--campaigns", default="", help="Comma-separated Campaign IDs to filter (e.g. Workflow_2002,Workflow_0902)")
    parser.add_argument("--connections-file", default="", help="Path to a saved LinkedIn network/connections HTML file")
    args = parser.parse_args()

    campaigns = [c.strip() for c in args.campaigns.split(",") if c.strip()] if args.campaigns else None
    connections_path = Path(args.connections_file).expanduser() if args.connections_file else None
    try:
        run_linkedin_manager(
            dry_run=args.dry_run,
            campaigns=campaigns,
            connections_path=connections_path,
        )
    except LinkedInManagerError as e:
        console.print(f"\n[bold red]LinkedIn Analyst Agent failed: {e}[/bold red]")
        sys.exit(1)
