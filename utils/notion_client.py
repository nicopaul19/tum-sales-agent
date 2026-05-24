"""
Notion API Client for uploading qualified leads.
"""
import math
import re
import requests as http_requests
from notion_client import Client
from rich.console import Console
from typing import Optional, List

from utils.config import NOTION_TOKEN, NOTION_DB_QUALIFIED_ID, NOTION_DB_ACCOUNTS_ID, NOTION_DB_CONTACTS_ID

# Notion API headers for raw requests (notion-client SDK lacks databases.query)
NOTION_API_VERSION = "2022-06-28"

console = Console()


def _p(name: str, prop_map: Optional[dict] = None) -> str:
    """Translate an expected property name to the actual Notion name via preflight map."""
    if prop_map and name in prop_map:
        return prop_map[name]
    return name


def _s(status: str, status_map: Optional[dict] = None) -> str:
    """Translate an expected status value to the actual Notion status via preflight map."""
    if status_map and status in status_map:
        return status_map[status]
    return status


def clean_value(value):
    """Clean a value for JSON serialization (handle NaN, None, etc.)."""
    if value is None:
        return ""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def get_notion_client() -> Optional[Client]:
    """Get authenticated Notion client."""
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not configured[/red]")
        return None
    return Client(auth=NOTION_TOKEN)


def upload_lead_to_notion(
    lead: dict,
    score: float,
    draft_message: str,
    reasoning: str = "",
    database_id: Optional[str] = None
) -> bool:
    """
    Upload a qualified lead to Notion database.

    Args:
        lead: Lead dict with company_name, person_name, linkedin_url, context, email
        score: AI-generated score (0-10)
        draft_message: AI-generated outreach message draft
        reasoning: AI-generated reasoning for the score
        database_id: Notion database ID (defaults to NOTION_DB_QUALIFIED_ID)

    Returns:
        True if successful, False otherwise
    """
    client = get_notion_client()
    if not client:
        return False

    database_id = database_id or NOTION_DB_QUALIFIED_ID
    if not database_id:
        console.print("[red]Error: No Notion database ID provided[/red]")
        return False

    try:
        # Clean all values
        company_name = clean_value(lead.get("company_name")) or "Unknown"
        company_domain = clean_value(lead.get("company_domain"))
        person_name = clean_value(lead.get("person_name"))
        linkedin_url = clean_value(lead.get("linkedin_url"))
        email = clean_value(lead.get("email"))
        context = clean_value(lead.get("context"))[:2000]
        source = clean_value(lead.get("source")) or "unknown"
        draft_msg = clean_value(draft_message)[:2000]
        reasoning_text = clean_value(reasoning)[:2000]

        # Handle score - ensure it's a valid number
        if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
            score = 0.0

        properties = {
            "Company": {
                "title": [{"text": {"content": company_name}}]
            },
            "Domain": {
                "rich_text": [{"text": {"content": company_domain}}]
            },
            "Contact": {
                "rich_text": [{"text": {"content": person_name}}]
            },
            "Score": {
                "number": float(score)
            },
            "Reasoning": {
                "rich_text": [{"text": {"content": reasoning_text}}]
            },
            "Draft Message": {
                "rich_text": [{"text": {"content": draft_msg}}]
            },
            "Source": {
                "select": {"name": source}
            },
            "Status": {
                "select": {"name": "Ready for Review"}
            }
        }

        # Only add LinkedIn if valid URL
        if linkedin_url and linkedin_url.startswith("http"):
            properties["LinkedIn"] = {"url": linkedin_url}

        # Only add Email if valid
        if email and "@" in email:
            properties["Email"] = {"email": email}

        client.pages.create(
            parent={"database_id": database_id},
            properties=properties
        )

        console.print(f"[green]Uploaded to Notion: {lead.get('company_name')}[/green]")
        return True

    except Exception as e:
        console.print(f"[red]Notion upload error: {e}[/red]")
        return False


def upload_batch_to_notion(leads: list) -> int:
    """
    Upload multiple leads to Notion.

    Args:
        leads: List of tuples (lead_dict, score, draft_message)

    Returns:
        Number of successfully uploaded leads
    """
    success_count = 0

    for lead, score, draft_message in leads:
        if upload_lead_to_notion(lead, score, draft_message):
            success_count += 1

    console.print(f"[cyan]Uploaded {success_count}/{len(leads)} leads to Notion[/cyan]")
    return success_count


def _notion_api_headers() -> dict:
    """Get headers for raw Notion API requests."""
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION
    }


def get_existing_accounts_from_notion(database_id: Optional[str] = None, prop_map: Optional[dict] = None) -> dict:
    """
    Fetch all existing companies from Notion Accounts database.

    Uses raw Notion API since the Python SDK lacks databases.query().

    Notion Accounts DB schema:
        - "Organization*" (title) → company name
        - "Website URL*" (url) → company website
        - "Trigger Event" (rich_text) → outreach trigger

    Args:
        database_id: Notion Accounts database ID (defaults to NOTION_DB_ACCOUNTS_ID)

    Returns:
        Dict with normalized company names and domains as keys, mapping to page data.
    """
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not configured[/red]")
        return {"company_names": {}, "domains": {}}

    database_id = database_id or NOTION_DB_ACCOUNTS_ID
    if not database_id:
        console.print("[yellow]Warning: NOTION_DB_ACCOUNTS_ID not configured, skipping duplicate check[/yellow]")
        return {"company_names": {}, "domains": {}}

    try:
        console.print(f"[cyan]Fetching existing companies from Notion Accounts database...[/cyan]")

        headers = _notion_api_headers()
        url = f"https://api.notion.com/v1/databases/{database_id}/query"

        # Paginate through all results
        results = []
        has_more = True
        start_cursor = None

        while has_more:
            body = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            response = http_requests.post(url, headers=headers, json=body)
            if response.status_code != 200:
                console.print(f"[red]Notion API error: {response.status_code} - {response.json().get('message', '')}[/red]")
                return {"company_names": {}, "domains": {}}

            data = response.json()
            results.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        # Build lookup dictionaries
        company_names = {}
        domains = {}

        for page in results:
            properties = page.get("properties", {})

            # Extract company name from "Organization*" (title property)
            company_name = None
            for prop_name, prop_data in properties.items():
                if prop_data.get("type") == "title":
                    title_array = prop_data.get("title", [])
                    if title_array:
                        company_name = title_array[0].get("plain_text", "")
                    break

            # Extract domain from "Website URL*" (url property)
            domain = None
            website_prop = properties.get(_p("Website URL*", prop_map), {})
            if website_prop.get("type") == "url" and website_prop.get("url"):
                raw_url = website_prop["url"]
                # Normalize: extract domain from full URL
                match = re.search(r'(?:https?://)?(?:www\.)?([^/\s]+)', raw_url.lower())
                if match:
                    domain = match.group(1).rstrip("/")

            # Extract account status
            status_prop = properties.get(_p("Status", prop_map), {})
            account_status = None
            if status_prop.get("type") == "status" and status_prop.get("status"):
                account_status = status_prop["status"].get("name")

            # Extract Campaign IDs (multi_select)
            campaign_ids = []
            campaign_prop = properties.get(_p("Campaign ID", prop_map), {})
            if campaign_prop.get("type") == "multi_select":
                campaign_ids = [ms.get("name", "") for ms in campaign_prop.get("multi_select", [])]

            page_data = {
                "page_id": page.get("id"),
                "company_name": company_name,
                "domain": domain,
                "status": account_status,
                "campaign_ids": campaign_ids,
                "url": page.get("url")
            }

            # Store in lookup dicts
            if company_name:
                normalized_name = company_name.lower().strip()
                company_names[normalized_name] = page_data

            if domain:
                domains[domain] = page_data

        console.print(f"[green]Found {len(company_names)} existing companies in Notion Accounts[/green]")
        return {"company_names": company_names, "domains": domains}

    except Exception as e:
        console.print(f"[red]Error fetching Notion Accounts: {e}[/red]")
        return {"company_names": {}, "domains": {}}


def get_pipeline_success_companies() -> List[dict]:
    """
    Fetch companies from Workflow campaigns that reached Engaged (idx 7) or above.

    These are our proven success stories — companies that responded well to our
    outreach. Used by ranking_agent to boost lookalike companies.

    Returns:
        List of dicts with company_name, domain, status, account_type, trigger.
    """
    from agents.notion_cleanup import STATUS_HIERARCHY

    if not NOTION_TOKEN or not NOTION_DB_ACCOUNTS_ID:
        return []

    try:
        headers = _notion_api_headers()
        url = f"https://api.notion.com/v1/databases/{NOTION_DB_ACCOUNTS_ID}/query"

        results = []
        has_more = True
        start_cursor = None

        while has_more:
            body = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = http_requests.post(url, headers=headers, json=body, timeout=15)
            if response.status_code != 200:
                return []
            data = response.json()
            results.extend(data.get("results", []))
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        successes = []
        for page in results:
            props = page.get("properties", {})

            # Check campaign ID — only Workflow_* campaigns
            campaign_prop = props.get("Campaign ID", {})
            campaigns = []
            if campaign_prop.get("type") == "multi_select":
                campaigns = [ms.get("name", "") for ms in campaign_prop.get("multi_select", [])]
            has_workflow = any(c.startswith("Workflow_") for c in campaigns)
            if not has_workflow:
                continue

            # Check status >= Engaged (index 7)
            status_prop = props.get("Status", {})
            status = None
            if status_prop.get("type") == "status" and status_prop.get("status"):
                status = status_prop["status"].get("name")
            if not status or status not in STATUS_HIERARCHY:
                continue
            if STATUS_HIERARCHY.index(status) < 7:
                continue
            # Exclude "Prospect Unqualified" (idx 12)
            if STATUS_HIERARCHY.index(status) == 12:
                continue

            # Extract company name
            company_name = None
            for prop_name, prop_data in props.items():
                if prop_data.get("type") == "title":
                    title_array = prop_data.get("title", [])
                    if title_array:
                        company_name = title_array[0].get("plain_text", "")
                    break

            # Extract domain
            domain = None
            website_prop = props.get("Website URL*", {})
            if website_prop.get("type") == "url" and website_prop.get("url"):
                raw_url = website_prop["url"]
                match = re.search(r'(?:https?://)?(?:www\.)?([^/\s]+)', raw_url.lower())
                if match:
                    domain = match.group(1).rstrip("/")

            # Extract account type
            account_type = None
            type_prop = props.get("Account Type*", {})
            if type_prop.get("type") == "select" and type_prop.get("select"):
                account_type = type_prop["select"].get("name")

            # Extract trigger
            trigger = ""
            trigger_prop = props.get("Trigger Event", {})
            if trigger_prop.get("type") == "rich_text":
                texts = trigger_prop.get("rich_text", [])
                if texts:
                    trigger = texts[0].get("plain_text", "")

            if company_name:
                successes.append({
                    "company_name": company_name,
                    "domain": domain or "",
                    "status": status,
                    "account_type": account_type or "",
                    "trigger": trigger[:100],
                })

        console.print(f"[cyan]Pipeline success: {len(successes)} companies reached Engaged+ from Workflow campaigns[/cyan]")
        return successes

    except Exception as e:
        console.print(f"[yellow]Could not fetch pipeline successes: {e}[/yellow]")
        return []


def get_existing_contacts_from_notion(database_id: Optional[str] = None) -> dict:
    """
    Fetch all contacts from the Notion Contacts database.

    Paginates through all contacts and extracts name, LinkedIn URL, and email.

    Args:
        database_id: Contacts database ID (defaults to NOTION_DB_CONTACTS_ID)

    Returns:
        Dict with:
            "names": set of lowercase contact names
            "linkedin_urls": set of LinkedIn profile URLs
            "emails": set of lowercase emails
    """
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not configured[/red]")
        return {"names": set(), "linkedin_urls": set(), "emails": set()}

    database_id = database_id or NOTION_DB_CONTACTS_ID
    if not database_id:
        console.print("[yellow]Warning: NOTION_DB_CONTACTS_ID not configured, skipping contact dedup[/yellow]")
        return {"names": set(), "linkedin_urls": set(), "emails": set()}

    try:
        console.print("[cyan]Fetching existing contacts from Notion Contacts database...[/cyan]")

        headers = _notion_api_headers()
        url = f"https://api.notion.com/v1/databases/{database_id}/query"

        names = set()
        linkedin_urls = set()
        emails = set()
        has_more = True
        start_cursor = None

        while has_more:
            body = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            response = http_requests.post(url, headers=headers, json=body)
            if response.status_code != 200:
                console.print(f"[red]Notion API error: {response.status_code} - {response.json().get('message', '')}[/red]")
                return {"names": set(), "linkedin_urls": set(), "emails": set()}

            data = response.json()

            for page in data.get("results", []):
                properties = page.get("properties", {})

                # Extract contact name from title property
                for prop_name, prop_data in properties.items():
                    if prop_data.get("type") == "title":
                        title_array = prop_data.get("title", [])
                        if title_array:
                            name = title_array[0].get("plain_text", "").strip()
                            if name:
                                names.add(name.lower())
                        break

                # Extract LinkedIn URL
                linkedin_prop = properties.get("LinkedIn", {})
                if linkedin_prop.get("type") == "url" and linkedin_prop.get("url"):
                    linkedin_urls.add(linkedin_prop["url"].rstrip("/"))

                # Extract email
                email_prop = properties.get("Email", {})
                if email_prop.get("type") == "email" and email_prop.get("email"):
                    emails.add(email_prop["email"].lower().strip())

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        console.print(f"[green]Found {len(names)} existing contacts in Notion ({len(emails)} with email)[/green]")
        return {"names": names, "linkedin_urls": linkedin_urls, "emails": emails}

    except Exception as e:
        console.print(f"[red]Error fetching Notion Contacts: {e}[/red]")
        return {"names": set(), "linkedin_urls": set(), "emails": set()}


def get_existing_contact_emails_from_notion(database_id: Optional[str] = None) -> set:
    """
    Fetch all email addresses from the Notion Contacts database.

    Paginates through all contacts and extracts the "Email" property.

    Args:
        database_id: Contacts database ID (defaults to NOTION_DB_CONTACTS_ID)

    Returns:
        Set of lowercase email strings for fast lookup.
    """
    if not NOTION_TOKEN:
        console.print("[red]Error: NOTION_TOKEN not configured[/red]")
        return set()

    database_id = database_id or NOTION_DB_CONTACTS_ID
    if not database_id:
        console.print("[yellow]Warning: NOTION_DB_CONTACTS_ID not configured, skipping email dedup[/yellow]")
        return set()

    try:
        console.print("[cyan]Fetching existing contact emails from Notion Contacts database...[/cyan]")

        headers = _notion_api_headers()
        url = f"https://api.notion.com/v1/databases/{database_id}/query"

        emails = set()
        has_more = True
        start_cursor = None

        while has_more:
            body = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            response = http_requests.post(url, headers=headers, json=body)
            if response.status_code != 200:
                console.print(f"[red]Notion API error: {response.status_code} - {response.json().get('message', '')}[/red]")
                return set()

            data = response.json()

            for page in data.get("results", []):
                properties = page.get("properties", {})
                email_prop = properties.get("Email", {})
                if email_prop.get("type") == "email" and email_prop.get("email"):
                    emails.add(email_prop["email"].lower().strip())

            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        console.print(f"[green]Found {len(emails)} existing contact emails in Notion[/green]")
        return emails

    except Exception as e:
        console.print(f"[red]Error fetching Notion Contacts: {e}[/red]")
        return set()


def is_status_engaged_or_above(status: str, status_map: Optional[dict] = None) -> bool:
    """
    Check if an account status is at or above "Engaged" in the hierarchy.

    Uses STATUS_HIERARCHY from notion_cleanup: index 7 ("Engaged") and above
    are considered high-engagement statuses that should block re-qualification.

    Args:
        status: The Notion account status name
        status_map: Optional preflight status map for name translation

    Returns:
        True if status is "Engaged" or higher (index >= 7), False otherwise.
    """
    from agents.notion_cleanup import STATUS_HIERARCHY

    ENGAGED_INDEX = 7

    if not status:
        return False

    # If we have a status_map, reverse-lookup the canonical name
    if status_map:
        # Build reverse map: actual_name → expected_name
        reverse = {v: k for k, v in status_map.items()}
        canonical = reverse.get(status, status)
    else:
        canonical = status

    try:
        idx = STATUS_HIERARCHY.index(canonical)
        return idx >= ENGAGED_INDEX
    except ValueError:
        # Unknown status — treat as not engaged (allow re-qualification)
        return False


def update_trigger_in_notion(page_id: str, new_trigger: str) -> bool:
    """
    Update the "Trigger Event" field for an existing Notion Accounts page.

    Args:
        page_id: Notion page ID to update
        new_trigger: New trigger text to append

    Returns:
        True if successful, False otherwise
    """
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()

    try:
        # Fetch current page to get existing trigger
        resp = http_requests.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers
        )
        if resp.status_code != 200:
            return False

        page = resp.json()
        properties = page.get("properties", {})

        # Get current trigger value from "Trigger Event" (rich_text)
        trigger_prop = properties.get("Trigger Event", {})
        current_trigger = ""
        if trigger_prop.get("type") == "rich_text":
            texts = trigger_prop.get("rich_text", [])
            if texts:
                current_trigger = texts[0].get("plain_text", "")

        # Append new trigger if it's not already there
        if new_trigger and new_trigger not in current_trigger:
            updated_trigger = f"{current_trigger}\n{new_trigger}".strip()

            resp = http_requests.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=headers,
                json={
                    "properties": {
                        "Trigger Event": {
                            "rich_text": [{"text": {"content": updated_trigger}}]
                        }
                    }
                }
            )

            if resp.status_code == 200:
                console.print(f"[green]  Updated trigger in Notion: {page_id[:8]}...[/green]")
                return True
            else:
                console.print(f"[red]Notion update error: {resp.status_code}[/red]")
                return False

        return True  # Trigger already exists, no update needed

    except Exception as e:
        console.print(f"[red]Error updating Notion page: {e}[/red]")
        return False


# Statuses that indicate a stale lead that should be re-qualified on new trigger
STALE_STATUSES = {
    "Connect. Request sent",
    "Contact details wrong",
    "Voicemail sent",
    "Nurture",
}


def reset_account_status_if_stale(page_id: str, current_status: str, company_name: str = "") -> bool:
    """
    Reset account status to "Prospect Qualified" if it's in a stale state.

    Args:
        page_id: Notion page ID of the account
        current_status: Current status name
        company_name: Company name for logging

    Returns:
        True if status was reset, False otherwise
    """
    if not NOTION_TOKEN or not current_status:
        return False

    if current_status not in STALE_STATUSES:
        return False

    headers = _notion_api_headers()

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers,
            json={
                "properties": {
                    "Status": {
                        "status": {"name": "Prospect Qualified"}
                    }
                }
            }
        )

        if resp.status_code == 200:
            console.print(f"[green]  Status reset: {company_name} '{current_status}' → 'Prospect Qualified'[/green]")
            return True
        else:
            console.print(f"[red]  Status reset failed for {company_name}: {resp.status_code}[/red]")
            return False

    except Exception as e:
        console.print(f"[red]Error resetting status: {e}[/red]")
        return False


def create_contact_in_notion(
    person_name: str,
    linkedin_url: str = "",
    email: str = "",
    account_page_id: str = "",
    job_title: str = "",
    phone: str = "",
    apollo_contact_id: str = "",
    campaign_sender: str = "",
    database_id: Optional[str] = None,
    prop_map: Optional[dict] = None,
) -> bool:
    """
    Create a contact in the Notion Contacts database, linked to an Account.

    Contacts DB schema:
        - "Contact Name" (title)
        - "LinkedIn" (url)
        - "Email" (email)
        - "Job Title" (rich_text)
        - "Phone" (phone_number)
        - "Apollo Contact ID" (rich_text)
        - "Accounts" (relation → Accounts DB)

    Args:
        person_name: Contact's full name
        linkedin_url: LinkedIn profile URL
        email: Email address
        account_page_id: Notion page ID of the linked Account
        job_title: Contact's job title
        phone: Contact's phone number
        apollo_contact_id: Apollo's contact ID
        database_id: Contacts database ID (defaults to NOTION_DB_CONTACTS_ID)

    Returns:
        True if successful, False otherwise
    """
    if not NOTION_TOKEN:
        return False

    database_id = database_id or NOTION_DB_CONTACTS_ID
    if not database_id:
        console.print("[yellow]Warning: NOTION_DB_CONTACTS_ID not configured[/yellow]")
        return False

    if not person_name or person_name in ("nan", ""):
        return False

    headers = _notion_api_headers()

    try:
        properties = {
            _p("Contact Name", prop_map): {
                "title": [{"text": {"content": person_name}}]
            }
        }

        # Add LinkedIn URL if valid
        if linkedin_url and linkedin_url.startswith("http"):
            properties[_p("LinkedIn", prop_map)] = {"url": linkedin_url}

        # Add email if valid
        if email and "@" in str(email) and str(email) != "nan":
            properties[_p("Email", prop_map)] = {"email": str(email)}

        # Link to Account via relation
        if account_page_id:
            properties[_p("Accounts", prop_map)] = {
                "relation": [{"id": account_page_id}]
            }

        # Job Title (rich_text)
        if job_title and job_title != "nan":
            properties[_p("Job Title", prop_map)] = {"rich_text": [{"text": {"content": job_title}}]}

        # Phone (phone_number)
        if phone and phone != "nan":
            properties[_p("Phone", prop_map)] = {"phone_number": phone}

        # Apollo Contact ID (rich_text)
        if apollo_contact_id and apollo_contact_id != "nan":
            properties[_p("Apollo Contact ID", prop_map)] = {"rich_text": [{"text": {"content": apollo_contact_id}}]}

        if campaign_sender and campaign_sender != "nan":
            properties[_p("Campaign Sender", prop_map)] = {"rich_text": [{"text": {"content": campaign_sender}}]}

        resp = http_requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={
                "parent": {"database_id": database_id},
                "properties": properties
            }
        )

        if resp.status_code == 200:
            console.print(f"[green]  Created contact: {person_name}[/green]")
            return True
        else:
            err = resp.json().get("message", resp.text)
            console.print(f"[red]  Contact creation failed: {resp.status_code} - {err}[/red]")
            return False

    except Exception as e:
        console.print(f"[red]Error creating contact: {e}[/red]")
        return False


def ensure_notion_properties(accounts_db_id: str, contacts_db_id: str) -> bool:
    """
    Ensure new Apollo-related properties exist in Notion databases.

    Uses PATCH /v1/databases/{id} which is idempotent — Notion ignores
    properties that already exist.

    New Accounts properties: Apollo Account ID, # Employees, Latest Funding,
        Funding Amount, Lead Score, Company LinkedIn
    New Contacts properties: Apollo Contact ID

    Returns:
        True if both updates succeed, False otherwise.
    """
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()

    # Accounts DB — new properties
    accounts_props = {
        "Apollo Account ID": {"rich_text": {}},
        "# Employees": {"number": {"format": "number"}},
        "Latest Funding": {"rich_text": {}},
        "Funding Amount": {"number": {"format": "number"}},
        "Lead Score": {"number": {"format": "number"}},
        "Company LinkedIn": {"url": {}},
        "Campaign Sender": {"rich_text": {}},
        "[Suspect] Contact Name": {"rich_text": {}},
        "[Suspect] Contact LinkedIn URL": {"url": {}},
        "[Suspect] Contact Email": {"rich_text": {}},
        "[Suspect] Job Title": {"rich_text": {}},
        "[Suspect] Contact Phone": {"rich_text": {}},
        "LinkedIn 1st Cold": {"rich_text": {}},
        "LinkedIn FU message": {"rich_text": {}},
        "Cold Email Body": {"rich_text": {}},
        "Cold Email Subject Text": {"rich_text": {}},
        "AB Variant": {"select": {"options": [{"name": "A"}, {"name": "B"}]}},
    }

    # Contacts DB — new properties
    contacts_props = {
        "Apollo Contact ID": {"rich_text": {}},
        "Campaign Sender": {"rich_text": {}},
    }

    success = True

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/databases/{accounts_db_id}",
            headers=headers,
            json={"properties": accounts_props}
        )
        if resp.status_code == 200:
            console.print("[green]Accounts DB properties ensured[/green]")
        else:
            console.print(f"[red]Failed to update Accounts DB properties: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            success = False
    except Exception as e:
        console.print(f"[red]Error updating Accounts DB: {e}[/red]")
        success = False

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/databases/{contacts_db_id}",
            headers=headers,
            json={"properties": contacts_props}
        )
        if resp.status_code == 200:
            console.print("[green]Contacts DB properties ensured[/green]")
        else:
            console.print(f"[red]Failed to update Contacts DB properties: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            success = False
    except Exception as e:
        console.print(f"[red]Error updating Contacts DB: {e}[/red]")
        success = False

    return success


def get_account_page_properties(page_id: str) -> dict:
    """
    Fetch a single Notion page and return its properties dict.

    Used to check which fields are empty before updating.

    Args:
        page_id: The Notion page ID.

    Returns:
        Properties dict from the page, or empty dict on error.
    """
    if not NOTION_TOKEN:
        return {}

    headers = _notion_api_headers()

    try:
        resp = http_requests.get(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers
        )
        if resp.status_code == 200:
            return resp.json().get("properties", {})
        else:
            console.print(f"[red]Failed to fetch page {page_id[:8]}...: {resp.status_code}[/red]")
            return {}
    except Exception as e:
        console.print(f"[red]Error fetching page: {e}[/red]")
        return {}


def _prop_is_empty(prop: dict) -> bool:
    """Check if a Notion property value is empty/unset."""
    ptype = prop.get("type", "")

    if ptype == "rich_text":
        texts = prop.get("rich_text", [])
        return not texts or not texts[0].get("plain_text", "").strip()
    elif ptype == "url":
        return not prop.get("url")
    elif ptype == "number":
        return prop.get("number") is None
    elif ptype == "select":
        return not prop.get("select")
    elif ptype == "multi_select":
        return not prop.get("multi_select")
    elif ptype == "email":
        return not prop.get("email")
    elif ptype == "phone_number":
        return not prop.get("phone_number")
    elif ptype == "title":
        titles = prop.get("title", [])
        return not titles or not titles[0].get("plain_text", "").strip()
    elif ptype == "status":
        return not prop.get("status")

    return True


def create_account_in_notion(
    data: dict,
    campaign_id: str,
    database_id: str,
    prop_map: Optional[dict] = None,
    status_map: Optional[dict] = None,
) -> str:
    """
    Create a new account page in the Notion Accounts database.

    Args:
        data: Dict with Apollo field values (see ACCOUNT_FIELD_MAP in upload_agent).
        campaign_id: Campaign tag, e.g. "Workflow_0902".
        database_id: Notion Accounts database ID.

    Returns:
        The new page_id string, or "" on failure.
    """
    if not NOTION_TOKEN:
        return ""

    headers = _notion_api_headers()

    properties = {
        _p("Organization*", prop_map): {
            "title": [{"text": {"content": clean_value(data.get("company_name")) or "Unknown"}}]
        },
        _p("Status", prop_map): {
            "status": {"name": _s("Prospect Qualified", status_map)}
        },
    }

    # Campaign ID (multi_select)
    if campaign_id:
        properties[_p("Campaign ID", prop_map)] = {
            "multi_select": [{"name": campaign_id}]
        }

    # Cleaned Name (rich_text)
    v = clean_value(data.get("cleaned_name"))
    if v:
        properties[_p("Cleaned Name*", prop_map)] = {"rich_text": [{"text": {"content": v}}]}

    # Website URL (url)
    v = clean_value(data.get("website"))
    if v and (v.startswith("http") or "." in v):
        if not v.startswith("http"):
            v = "https://" + v
        properties[_p("Website URL*", prop_map)] = {"url": v}

    # Company LinkedIn (url)
    v = clean_value(data.get("company_linkedin"))
    if v and v.startswith("http"):
        properties[_p("Company LinkedIn", prop_map)] = {"url": v}

    # City (select)
    v = clean_value(data.get("city"))
    if v:
        properties[_p("City", prop_map)] = {"select": {"name": v}}

    # Country (multi_select)
    v = clean_value(data.get("country"))
    if v:
        properties[_p("Country", prop_map)] = {"multi_select": [{"name": v}]}

    # Company Phone (phone_number)
    v = clean_value(data.get("company_phone"))
    if v:
        properties[_p("Company Phone Number", prop_map)] = {"phone_number": v}

    # Industry (rich_text)
    v = clean_value(data.get("industry"))
    if v:
        properties[_p("Industry (Corporates)", prop_map)] = {"rich_text": [{"text": {"content": v}}]}

    # Trigger Event (rich_text)
    v = clean_value(data.get("trigger"))
    if v:
        properties[_p("Trigger Event", prop_map)] = {"rich_text": [{"text": {"content": v}}]}

    # Mission (rich_text)
    v = clean_value(data.get("mission"))
    if v:
        properties[_p("Mission*", prop_map)] = {"rich_text": [{"text": {"content": v[:2000]}}]}

    # Lead Score (number)
    v = data.get("lead_score")
    if v is not None and v != "" and not (isinstance(v, float) and math.isnan(v)):
        try:
            properties[_p("Lead Score", prop_map)] = {"number": float(v)}
        except (ValueError, TypeError):
            pass

    # # Employees (number)
    v = data.get("employees")
    if v is not None and v != "" and not (isinstance(v, float) and math.isnan(v)):
        try:
            properties[_p("# Employees", prop_map)] = {"number": float(v)}
        except (ValueError, TypeError):
            pass

    # Latest Funding (rich_text)
    v = clean_value(data.get("latest_funding"))
    if v:
        properties[_p("Latest Funding", prop_map)] = {"rich_text": [{"text": {"content": v}}]}

    # Funding Amount (number)
    v = data.get("funding_amount")
    if v is not None and v != "" and not (isinstance(v, float) and math.isnan(v)):
        try:
            properties[_p("Funding Amount", prop_map)] = {"number": float(v)}
        except (ValueError, TypeError):
            pass

    # Apollo Account ID (rich_text)
    v = clean_value(data.get("apollo_account_id"))
    if v:
        properties[_p("Apollo Account ID", prop_map)] = {"rich_text": [{"text": {"content": v}}]}

    account_contact_fields = [
        ("person_name", "[Suspect] Contact Name", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("linkedin_url", "[Suspect] Contact LinkedIn URL", lambda v: {"url": v} if v.startswith("http") else None),
        ("email", "[Suspect] Contact Email", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("job_title", "[Suspect] Job Title", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("phone", "[Suspect] Contact Phone", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("campaign_sender", "Campaign Sender", lambda v: {"rich_text": [{"text": {"content": v}}]}),
    ]
    for key, prop_name, builder in account_contact_fields:
        v = clean_value(data.get(key))
        if v:
            result = builder(v)
            if result:
                properties[_p(prop_name, prop_map)] = result

    try:
        resp = http_requests.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={
                "parent": {"database_id": database_id},
                "properties": properties
            }
        )

        if resp.status_code == 200:
            page_id = resp.json().get("id", "")
            console.print(f"[green]  Created account: {data.get('company_name')}[/green]")
            return page_id
        else:
            err = resp.json().get("message", resp.text)
            console.print(f"[red]  Account creation failed: {resp.status_code} - {err}[/red]")
            return ""

    except Exception as e:
        console.print(f"[red]Error creating account: {e}[/red]")
        return ""


def update_account_in_notion(
    page_id: str,
    data: dict,
    reset_status: bool,
    campaign_id: str = "",
    prop_map: Optional[dict] = None,
    status_map: Optional[dict] = None,
) -> bool:
    """
    Update an existing account page — only fills empty fields, appends trigger.

    Args:
        page_id: Notion page ID of the existing account.
        data: Dict with Apollo field values.
        reset_status: If True, set Status to "Prospect Qualified".
        campaign_id: Campaign tag to add to multi_select Campaign ID.

    Returns:
        True if successful, False otherwise.
    """
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()

    # Fetch current properties to check which are empty
    current_props = get_account_page_properties(page_id)
    if not current_props:
        return False

    updates = {}

    # Status reset
    if reset_status:
        updates[_p("Status", prop_map)] = {"status": {"name": _s("Prospect Qualified", status_map)}}

    # Campaign ID — append to existing multi_select
    if campaign_id:
        existing_campaigns = []
        camp_prop = current_props.get(_p("Campaign ID", prop_map), {})
        if camp_prop.get("type") == "multi_select":
            existing_campaigns = [item["name"] for item in camp_prop.get("multi_select", [])]
        if campaign_id not in existing_campaigns:
            existing_campaigns.append(campaign_id)
            updates[_p("Campaign ID", prop_map)] = {
                "multi_select": [{"name": c} for c in existing_campaigns]
            }

    # Map of internal key → (expected Notion property name, builder function)
    fill_if_empty = [
        ("cleaned_name", "Cleaned Name*", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("website", "Website URL*", lambda v: {"url": v if v.startswith("http") else f"https://{v}"} if ("." in v) else None),
        ("company_linkedin", "Company LinkedIn", lambda v: {"url": v} if v.startswith("http") else None),
        ("city", "City", lambda v: {"select": {"name": v}}),
        ("country", "Country", lambda v: {"multi_select": [{"name": v}]}),
        ("company_phone", "Company Phone Number", lambda v: {"phone_number": v}),
        ("industry", "Industry (Corporates)", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("mission", "Mission*", lambda v: {"rich_text": [{"text": {"content": v[:2000]}}]}),
        ("lead_score", "Lead Score", None),
        ("employees", "# Employees", None),
        ("latest_funding", "Latest Funding", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("funding_amount", "Funding Amount", None),
        ("apollo_account_id", "Apollo Account ID", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("person_name", "[Suspect] Contact Name", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("linkedin_url", "[Suspect] Contact LinkedIn URL", lambda v: {"url": v} if v.startswith("http") else None),
        ("email", "[Suspect] Contact Email", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("job_title", "[Suspect] Job Title", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("phone", "[Suspect] Contact Phone", lambda v: {"rich_text": [{"text": {"content": v}}]}),
        ("campaign_sender", "Campaign Sender", lambda v: {"rich_text": [{"text": {"content": v}}]}),
    ]

    for key, prop_name, builder in fill_if_empty:
        # Only fill if current property is empty — use actual name from prop_map
        actual_name = _p(prop_name, prop_map)
        current = current_props.get(actual_name, {})
        if not _prop_is_empty(current):
            continue

        val = data.get(key)
        if val is None or (isinstance(val, str) and not val) or (isinstance(val, float) and math.isnan(val)):
            continue

        if builder is None:
            # Number field
            try:
                updates[actual_name] = {"number": float(val)}
            except (ValueError, TypeError):
                continue
        else:
            result = builder(clean_value(val))
            if result:
                updates[actual_name] = result

    # Trigger Event — always append, never overwrite
    new_trigger = clean_value(data.get("trigger"))
    if new_trigger:
        trigger_actual = _p("Trigger Event", prop_map)
        trigger_prop = current_props.get(trigger_actual, {})
        current_trigger = ""
        if trigger_prop.get("type") == "rich_text":
            texts = trigger_prop.get("rich_text", [])
            if texts:
                current_trigger = texts[0].get("plain_text", "")

        if new_trigger not in current_trigger:
            updated = f"{current_trigger}\n{new_trigger}".strip()
            updates[trigger_actual] = {"rich_text": [{"text": {"content": updated[:2000]}}]}

    if not updates:
        return True  # Nothing to update

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers,
            json={"properties": updates}
        )

        if resp.status_code == 200:
            company = data.get("company_name", page_id[:8])
            action = "reset + updated" if reset_status else "updated"
            console.print(f"[green]  Account {action}: {company}[/green]")
            return True
        else:
            err = resp.json().get("message", resp.text)
            console.print(f"[red]  Account update failed: {resp.status_code} - {err}[/red]")
            return False

    except Exception as e:
        console.print(f"[red]Error updating account: {e}[/red]")
        return False


# ---------------------------------------------------------------------------
# Copywriter Agent helpers
# ---------------------------------------------------------------------------

OUTREACH_CONTACT_PROPS = {
    "LinkedIn 1st Cold": {"rich_text": {}},
    "LinkedIn FU message": {"rich_text": {}},
    "Cold Email Body": {"rich_text": {}},
    "Cold Email Subject": {"rich_text": {}},
    "AB Variant": {"select": {"options": [{"name": "A"}, {"name": "B"}]}},
}


def ensure_contact_outreach_properties(contacts_db_id: str) -> bool:
    """
    Ensure outreach message properties exist on the Contacts DB.

    Creates: LinkedIn 1st Cold, LinkedIn FU message, Cold Email Body, Cold Email Subject
    Idempotent — Notion ignores existing properties.
    """
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/databases/{contacts_db_id}",
            headers=headers,
            json={"properties": OUTREACH_CONTACT_PROPS}
        )
        if resp.status_code == 200:
            console.print("[green]Contact outreach properties ensured[/green]")
            return True
        else:
            console.print(f"[red]Failed to create contact outreach props: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return False
    except Exception as e:
        console.print(f"[red]Error creating contact outreach props: {e}[/red]")
        return False


def get_contacts_for_copywriting(
    contacts_db_id: str,
    campaign_id: str = "",
    force: bool = False,
    prop_map: Optional[dict] = None,
) -> List[dict]:
    """
    Fetch contacts that need outreach messages generated.

    Filters by:
    - Campaign Source rollup contains campaign_id (if provided)
    - LinkedIn 1st Cold is empty (no message written yet) — unless force=True

    For each contact, also resolves the linked Account to pull company context.

    Args:
        contacts_db_id: Notion database ID for Contacts.
        campaign_id: Only process contacts from this campaign (empty = all).
        force: If True, fetch ALL contacts (even those with existing messages).

    Returns:
        List of dicts with contact + account data for message generation.
    """
    if not NOTION_TOKEN:
        return []

    headers = _notion_api_headers()
    url = f"https://api.notion.com/v1/databases/{contacts_db_id}/query"

    # Build filter: LinkedIn 1st Cold is empty (skip if force=True)
    query_filter = None if force else {
        "property": _p("LinkedIn 1st Cold", prop_map),
        "rich_text": {"is_empty": True}
    }

    results = []
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100}
        if query_filter:
            body["filter"] = query_filter
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            console.print(f"[red]Notion query error: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return []

        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    contacts = []

    for page in results:
        props = page.get("properties", {})
        contact_page_id = page.get("id", "")

        # Extract contact name
        contact_name = ""
        for pname, pdata in props.items():
            if pdata.get("type") == "title":
                titles = pdata.get("title", [])
                if titles:
                    contact_name = titles[0].get("plain_text", "")
                break

        # Extract email
        email = ""
        email_prop = props.get(_p("Email", prop_map), {})
        if email_prop.get("type") == "email" and email_prop.get("email"):
            email = email_prop["email"]

        # Extract LinkedIn URL
        linkedin = ""
        li_prop = props.get(_p("LinkedIn", prop_map), {})
        if li_prop.get("type") == "url" and li_prop.get("url"):
            linkedin = li_prop["url"]

        # Extract Job Title
        job_title = ""
        jt_prop = props.get(_p("Job Title", prop_map), {})
        if jt_prop.get("type") == "rich_text" and jt_prop.get("rich_text"):
            job_title = jt_prop["rich_text"][0].get("plain_text", "")

        # Extract linked Account page_id from relation
        account_page_id = ""
        account_rel = props.get(_p("Accounts", prop_map), {})
        if account_rel.get("type") == "relation":
            relations = account_rel.get("relation", [])
            if relations:
                account_page_id = relations[0].get("id", "")

        # Check Campaign ID rollup (if filtering by campaign)
        if campaign_id:
            campaign_prop = props.get(_p("Campaign ID", prop_map), {})
            if campaign_prop.get("type") == "rollup":
                rollup = campaign_prop.get("rollup", {})
                rollup_array = rollup.get("array", [])
                # Rollup of multi_select → array of multi_select values
                campaign_names = set()
                for item in rollup_array:
                    if item.get("type") == "multi_select":
                        for ms in item.get("multi_select", []):
                            campaign_names.add(ms.get("name", ""))
                if campaign_id not in campaign_names:
                    continue  # Skip contacts not in this campaign

        # Check Account Status rollup — skip contacts already contacted or further
        account_status = ""
        status_rollup = props.get(_p("Account Status", prop_map), {})
        if status_rollup.get("type") == "rollup":
            status_array = status_rollup.get("rollup", {}).get("array", [])
            for item in status_array:
                if item.get("type") == "status" and item.get("status"):
                    account_status = item["status"].get("name", "")
        SKIP_STATUSES = {
            "Contacted LinkedIn \U0001f310", "Contacted Email \U0001f4e7",
            "Engaged", "Meeting Scheduled \U0001f4c5",
            "Discovery Call Booked", "Proposal Sent",
            "Partner Confirmed \U0001f91d", "Mentorship confirmed",
            "Active Partner", "Churned", "Disqualified",
        }
        if account_status in SKIP_STATUSES and not force:
            console.print(f"  [yellow]Skipping {contact_name} — account status: {account_status}[/yellow]")
            continue

        # Fetch account data for context
        account_data = {}
        if account_page_id:
            account_data = _fetch_account_context(account_page_id, headers)

        contacts.append({
            "contact_page_id": contact_page_id,
            "person_name": contact_name,
            "email": email,
            "linkedin_url": linkedin,
            "job_title": job_title,
            "account_page_id": account_page_id,
            **account_data,
        })

    return contacts


def get_accounts_for_copywriting(
    accounts_db_id: str,
    campaign_id: str = "",
    force: bool = False,
    prop_map: Optional[dict] = None,
) -> List[dict]:
    """
    Fetch account-level contact records that need outreach copy.

    This is the fallback path when the Contacts database is not shared with the
    integration. It uses the [Suspect] contact fields and outreach properties
    directly on the Accounts database.
    """
    if not NOTION_TOKEN:
        return []

    headers = _notion_api_headers()
    url = f"https://api.notion.com/v1/databases/{accounts_db_id}/query"
    results = []
    has_more = True
    start_cursor = None

    while has_more:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = http_requests.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            console.print(f"[red]Accounts query error: {resp.status_code} - {resp.json().get('message', '')}[/red]")
            return []

        data = resp.json()
        results.extend(data.get("results", []))
        has_more = data.get("has_more", False)
        start_cursor = data.get("next_cursor")

    contacts = []
    skip_statuses = {
        "Contacted LinkedIn \U0001f310", "Contacted Email \U0001f4e7",
        "Contacted Mail \U0001f4e9", "Engaged", "Discovery Call Booked",
        "Proposal Sent", "Partner Confirmed \U0001f91d", "Active Partner",
        "Churned", "Disqualified", "Prospect Unqualified",
    }

    for page in results:
        props = page.get("properties", {})
        account_page_id = page.get("id", "")

        def _text(prop_name):
            p = props.get(_p(prop_name, prop_map), {})
            if p.get("type") == "rich_text" and p.get("rich_text"):
                return "".join(t.get("plain_text", "") for t in p.get("rich_text", []))
            if p.get("type") == "formula" and p.get("formula", {}).get("type") == "string":
                return p["formula"].get("string") or ""
            return ""

        def _title():
            for pdata in props.values():
                if pdata.get("type") == "title":
                    return "".join(t.get("plain_text", "") for t in pdata.get("title", []))
            return ""

        def _url(prop_name):
            p = props.get(_p(prop_name, prop_map), {})
            return p.get("url") or "" if p.get("type") == "url" else ""

        def _select(prop_name):
            p = props.get(_p(prop_name, prop_map), {})
            return p.get("select", {}).get("name", "") if p.get("type") == "select" and p.get("select") else ""

        def _status():
            p = props.get(_p("Status", prop_map), {})
            return p.get("status", {}).get("name", "") if p.get("type") == "status" and p.get("status") else ""

        def _multi_select(prop_name):
            p = props.get(_p(prop_name, prop_map), {})
            if p.get("type") == "multi_select":
                return [ms.get("name", "") for ms in p.get("multi_select", [])]
            return []

        def _number(prop_name):
            p = props.get(_p(prop_name, prop_map), {})
            return p.get("number") if p.get("type") == "number" else None

        account_campaigns = _multi_select("Campaign ID")
        if campaign_id and campaign_id not in account_campaigns:
            continue

        status = _status()
        if status in skip_statuses and not force:
            continue

        if not force and _text("LinkedIn 1st Cold"):
            continue

        person_name = _text("[Suspect] Contact Name").strip()
        linkedin_url = _url("[Suspect] Contact LinkedIn URL")
        email = _text("[Suspect] Contact Email").strip()
        # Fallback: use General Email if no [Suspect] contact info is set
        if not person_name and not linkedin_url and not email:
            general_email = _text("General Email").strip()
            if general_email:
                email = general_email
                person_name = ""   # will render as "there" / org-level greeting
            else:
                continue

        country_list = _multi_select("Country")
        contacts.append({
            "contact_page_id": account_page_id,
            "contact_storage": "account",
            "person_name": person_name or "",
            "email": email,
            "linkedin_url": linkedin_url,
            "job_title": _text("[Suspect] Job Title"),
            "account_page_id": account_page_id,
            "company_name": _title(),
            "website": _url("Website URL*"),
            "industry": _text("Industry (Corporates)"),
            "mission": _text("Mission*"),
            "trigger": _text("Trigger Event"),
            "account_type": _select("Account Type*"),
            "city": _select("City"),
            "country": country_list[0] if country_list else "",
            "company_description": _text("Company Description"),
            "latest_funding": _text("Latest Funding"),
            "employees": _number("# Employees"),
            "campaign_sender": _text("Campaign Sender"),
        })

    return contacts


def _fetch_account_context(account_page_id: str, headers: dict) -> dict:
    """Fetch relevant account fields for copywriting context."""
    try:
        resp = http_requests.get(
            f"https://api.notion.com/v1/pages/{account_page_id}",
            headers=headers
        )
        if resp.status_code != 200:
            return {}

        props = resp.json().get("properties", {})

        def _text(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "rich_text" and p.get("rich_text"):
                return p["rich_text"][0].get("plain_text", "")
            return ""

        def _title():
            for pname, pdata in props.items():
                if pdata.get("type") == "title":
                    titles = pdata.get("title", [])
                    if titles:
                        return titles[0].get("plain_text", "")
            return ""

        def _url(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "url":
                return p.get("url") or ""
            return ""

        def _select(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "select" and p.get("select"):
                return p["select"].get("name", "")
            return ""

        def _multi_select(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "multi_select":
                return [ms.get("name", "") for ms in p.get("multi_select", [])]
            return []

        def _number(prop_name):
            p = props.get(prop_name, {})
            if p.get("type") == "number" and p.get("number") is not None:
                return p["number"]
            return None

        country_list = _multi_select("Country")

        return {
            "company_name": _title(),
            "website": _url("Website URL*"),
            "industry": _text("Industry (Corporates)"),
            "mission": _text("Mission*"),
            "trigger": _text("Trigger Event"),
            "account_type": _select("Account Type*"),
            "city": _select("City"),
            "country": country_list[0] if country_list else "",
            "company_description": _text("Company Description"),
            "latest_funding": _text("Latest Funding"),
            "employees": _number("# Employees"),
        }

    except Exception as e:
        console.print(f"[red]Error fetching account context: {e}[/red]")
        return {}


def update_contact_outreach(
    contact_page_id: str,
    linkedin_first: str,
    linkedin_fu: str,
    email_body: str,
    email_subject: str,
    ab_variant: str = "",
    prop_map: Optional[dict] = None,
) -> bool:
    """
    Write the 4 outreach messages to a contact in Notion.

    Args:
        contact_page_id: Notion page ID of the contact.
        linkedin_first: Cold LinkedIn connection message.
        linkedin_fu: Follow-up LinkedIn message.
        email_body: Cold email body.
        email_subject: Cold email subject line.
        ab_variant: A/B test variant ("A" or "B"), empty to skip.

    Returns:
        True if successful, False otherwise.
    """
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()

    properties = {}

    if linkedin_first:
        properties[_p("LinkedIn 1st Cold", prop_map)] = {
            "rich_text": [{"text": {"content": linkedin_first[:2000]}}]
        }
    if linkedin_fu:
        properties[_p("LinkedIn FU message", prop_map)] = {
            "rich_text": [{"text": {"content": linkedin_fu[:2000]}}]
        }
    if email_body:
        properties[_p("Cold Email Body", prop_map)] = {
            "rich_text": [{"text": {"content": email_body[:2000]}}]
        }
    if email_subject:
        properties[_p("Cold Email Subject", prop_map)] = {
            "rich_text": [{"text": {"content": email_subject[:2000]}}]
        }
    if ab_variant in ("A", "B"):
        properties[_p("AB Variant", prop_map)] = {
            "select": {"name": ab_variant}
        }

    if not properties:
        return True

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/pages/{contact_page_id}",
            headers=headers,
            json={"properties": properties}
        )

        if resp.status_code == 200:
            return True
        else:
            err = resp.json().get("message", resp.text)
            console.print(f"[red]  Outreach update failed: {resp.status_code} - {err}[/red]")
            return False

    except Exception as e:
        console.print(f"[red]Error updating outreach: {e}[/red]")
        return False


def update_account_outreach(
    account_page_id: str,
    linkedin_first: str,
    linkedin_fu: str,
    email_body: str,
    email_subject: str,
    ab_variant: str = "",
    prop_map: Optional[dict] = None,
) -> bool:
    """Write outreach messages directly to an Account page fallback record."""
    if not NOTION_TOKEN:
        return False

    headers = _notion_api_headers()
    properties = {}

    if linkedin_first:
        properties[_p("LinkedIn 1st Cold", prop_map)] = {
            "rich_text": [{"text": {"content": linkedin_first[:2000]}}]
        }
    if linkedin_fu:
        properties[_p("LinkedIn FU message", prop_map)] = {
            "rich_text": [{"text": {"content": linkedin_fu[:2000]}}]
        }
    if email_body:
        properties[_p("Cold Email Body", prop_map)] = {
            "rich_text": [{"text": {"content": email_body[:2000]}}]
        }
    if email_subject:
        properties[_p("Cold Email Subject Text", prop_map)] = {
            "rich_text": [{"text": {"content": email_subject[:2000]}}]
        }
    if ab_variant in ("A", "B"):
        properties[_p("AB Variant", prop_map)] = {"select": {"name": ab_variant}}

    if not properties:
        return True

    try:
        resp = http_requests.patch(
            f"https://api.notion.com/v1/pages/{account_page_id}",
            headers=headers,
            json={"properties": properties}
        )
        if resp.status_code == 200:
            return True
        err = resp.json().get("message", resp.text)
        console.print(f"[red]  Account outreach update failed: {resp.status_code} - {err}[/red]")
        return False
    except Exception as e:
        console.print(f"[red]Error updating account outreach: {e}[/red]")
        return False
