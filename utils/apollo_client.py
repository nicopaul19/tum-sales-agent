"""
Apollo API Client for fetching leads from saved lists.
Minimal API usage to conserve credits.
"""
import requests
from typing import Optional
from rich.console import Console

from utils.config import APOLLO_API_KEY, APOLLO_LIST_ID_RAW

console = Console()

APOLLO_BASE_URL = "https://api.apollo.io/v1"


def get_list_contacts(list_id: Optional[str] = None, page: int = 1, per_page: int = 100) -> dict:
    """
    Fetch contacts from a saved Apollo list.

    Args:
        list_id: Apollo list ID (defaults to APOLLO_LIST_ID_RAW from config)
        page: Page number for pagination
        per_page: Number of results per page (max 100)

    Returns:
        dict with 'contacts' list and 'pagination' info
    """
    list_id = list_id or APOLLO_LIST_ID_RAW

    if not APOLLO_API_KEY:
        console.print("[red]Error: APOLLO_API_KEY not configured[/red]")
        return {"contacts": [], "pagination": {}}

    if not list_id:
        console.print("[red]Error: No Apollo list ID provided[/red]")
        return {"contacts": [], "pagination": {}}

    url = f"{APOLLO_BASE_URL}/contacts/search"

    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache"
    }

    payload = {
        "api_key": APOLLO_API_KEY,
        "contact_label_ids": [list_id],
        "page": page,
        "per_page": per_page
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()

        contacts = data.get("contacts", [])
        pagination = data.get("pagination", {})

        console.print(f"[green]Fetched {len(contacts)} contacts from Apollo[/green]")

        return {
            "contacts": contacts,
            "pagination": pagination
        }

    except requests.exceptions.RequestException as e:
        console.print(f"[red]Apollo API error: {e}[/red]")
        return {"contacts": [], "pagination": {}}


def parse_apollo_contact(contact: dict) -> dict:
    """
    Parse Apollo contact into our standard format.

    Args:
        contact: Raw Apollo contact dict

    Returns:
        Standardized contact dict matching our CSV schema
    """
    org = contact.get("organization", {}) or {}

    # Extract domain from Apollo's organization data
    domain = org.get("primary_domain", "") or ""
    if not domain:
        website = org.get("website_url", "")
        if website:
            from urllib.parse import urlparse
            parsed = urlparse(website if website.startswith("http") else f"https://{website}")
            domain = parsed.netloc.replace("www.", "")

    return {
        "source": "apollo_list_a",
        "company_name": org.get("name", ""),
        "company_domain": domain,
        "person_name": f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip(),
        "linkedin_url_contact": contact.get("linkedin_url", ""),
        "trigger": contact.get("headline", "") or contact.get("title", ""),
        "status": "pending",
        "apollo_id": contact.get("id", "")  # For deduplication tracking
    }


def fetch_all_new_contacts(processed_ids: set) -> list:
    """
    Fetch all contacts from Apollo list that haven't been processed yet.

    Args:
        processed_ids: Set of Apollo contact IDs already processed

    Returns:
        List of new contacts in standard format
    """
    all_contacts = []
    page = 1

    while True:
        result = get_list_contacts(page=page)
        contacts = result.get("contacts", [])

        if not contacts:
            break

        for contact in contacts:
            apollo_id = contact.get("id", "")
            if apollo_id and apollo_id not in processed_ids:
                parsed = parse_apollo_contact(contact)
                all_contacts.append(parsed)

        pagination = result.get("pagination", {})
        total_pages = pagination.get("total_pages", 1)

        if page >= total_pages:
            break

        page += 1

    console.print(f"[cyan]Found {len(all_contacts)} new contacts from Apollo[/cyan]")
    return all_contacts
